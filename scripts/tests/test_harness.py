# scripts/tests/test_harness.py
#
# The orchestrator, and the acquisition each probe does, against a drive
# which exists only in memory.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

import unittest

import fake

from greaseweazle.tools.probe import core, max_track, trk0


def context(usb=None, confirm=None, pause=None, **options):
    class Options:
        pass
    opts = Options()
    opts.max_cylinder = None
    for key, value in options.items():
        setattr(opts, key, value)
    return core.Context(usb or fake.FakeUnit(), opts,
                        confirm=confirm or (lambda p: True),
                        out=fake.Recorder(), pause=pause)


class TestOrchestrator(unittest.TestCase):
    """run_all: what runs, what is skipped, and what is recorded."""

    def test_a_probe_with_its_prerequisite_met_runs(self):
        first = fake.StubProbe('first')
        second = fake.StubProbe('second', depends_on=('first',))
        ctx = context()
        core.run_all(ctx, [first, second])
        self.assertTrue(first.ran)
        self.assertTrue(second.ran)

    def test_a_probe_whose_prerequisite_failed_is_skipped(self):
        first = fake.StubProbe('first', ok=False)
        second = fake.StubProbe('second', depends_on=('first',))
        ctx = context()
        core.run_all(ctx, [first, second])
        self.assertTrue(first.ran)
        self.assertFalse(second.ran)
        self.assertEqual(ctx.results['second'].status, 'skipped')

    def test_a_skip_records_which_prerequisite_failed(self):
        first = fake.StubProbe('first', ok=False)
        second = fake.StubProbe('second', depends_on=('first',))
        ctx = context()
        core.run_all(ctx, [first, second])
        self.assertIn('first', ctx.results['second'].as_dict()['reason'])

    def test_a_destructive_probe_is_not_run_without_consent(self):
        probe = fake.StubProbe('writes', destructive=True)
        ctx = context(confirm=lambda p: False)
        core.run_all(ctx, [probe])
        self.assertFalse(probe.ran)
        self.assertEqual(ctx.results['writes'].status, 'skipped')

    def test_consent_is_asked_once_per_destructive_probe(self):
        asked = []
        probe = fake.StubProbe('writes', destructive=True)
        ctx = context(confirm=lambda p: asked.append(p.name) or True)
        core.run_all(ctx, [probe])
        self.assertEqual(asked, ['writes'])

    def test_a_non_destructive_probe_is_never_asked_about(self):
        def refuse(probe):
            raise AssertionError('asked about a probe which writes nothing')
        ctx = context(confirm=refuse)
        core.run_all(ctx, [fake.StubProbe('reads')])

    def test_every_probe_leaves_a_result_behind(self):
        # The profile distinguishes "not measured" from "measured and
        # failed", which it can only do if skips are recorded rather than
        # omitted.
        first = fake.StubProbe('first', ok=False)
        second = fake.StubProbe('second', depends_on=('first',))
        ctx = context()
        core.run_all(ctx, [first, second])
        self.assertEqual(sorted(ctx.results), ['first', 'second'])
        self.assertIn('status', ctx.as_dict()['second'])


class TestMediaAnnouncements(unittest.TestCase):

    def test_the_requirement_is_announced_when_it_rises(self):
        ctx = context()
        core.run_all(ctx, [fake.StubProbe('a', needs_media=core.MEDIA_NONE),
                           fake.StubProbe('b', needs_media=core.MEDIA_ANY)])
        self.assertIn('No disk needed', ctx.report.text)
        self.assertIn('Load ANY disk', ctx.report.text)

    def test_it_is_announced_once_not_per_probe(self):
        ctx = context()
        core.run_all(ctx, [fake.StubProbe('a', needs_media=core.MEDIA_ANY),
                           fake.StubProbe('b', needs_media=core.MEDIA_ANY)])
        self.assertEqual(ctx.report.text.count('Load ANY disk'), 1)

    def test_the_run_pauses_for_the_disk_to_be_changed(self):
        asked = []
        ctx = context(pause=lambda prompt: asked.append(prompt))
        core.run_all(ctx, [fake.StubProbe('a', needs_media=core.MEDIA_NONE),
                           fake.StubProbe('b', needs_media=core.MEDIA_ANY)])
        self.assertEqual(len(asked), 2)

    def test_a_destructive_probe_is_not_paused_for_twice(self):
        # The consent gate already tells the user to load a scratch disk and
        # waits, so pausing again for the media change would ask twice.
        asked = []
        ctx = context(pause=lambda prompt: asked.append(prompt))
        core.run_all(ctx, [fake.StubProbe('w', destructive=True,
                                          needs_media=core.MEDIA_SCRATCH)])
        self.assertEqual(asked, [])


class TestTrk0Acquisition(unittest.TestCase):
    """The walk itself, against sensors which cannot be bought."""

    def test_a_healthy_sensor(self):
        result = trk0.measure(fake.FakeUnit(trk0=fake.TRK0_WORKING))
        self.assertEqual(result.status, trk0.OK)

    def test_a_sensor_stuck_asserted(self):
        result = trk0.measure(fake.FakeUnit(trk0=fake.TRK0_STUCK))
        self.assertEqual(result.status, trk0.STUCK_ASSERTED)

    def test_a_dead_sensor(self):
        result = trk0.measure(fake.FakeUnit(trk0=fake.TRK0_DEAD))
        self.assertEqual(result.status, trk0.ABSENT_AT_HOME)

    def test_a_sensor_which_answers_only_on_the_way_out(self):
        result = trk0.measure(fake.FakeUnit(trk0=fake.TRK0_ONE_WAY))
        self.assertEqual(result.status, trk0.NO_REASSERT)

    def test_the_head_is_left_at_cylinder_zero(self):
        usb = fake.FakeUnit()
        trk0.measure(usb)
        self.assertEqual(usb.head_cylinder, 0)
        self.assertEqual(usb.firmware_cylinder, 0)


class TestMaxTrackAcquisition(unittest.TestCase):
    """The search, against drives of known size and known bad habits."""

    def test_a_forty_cylinder_drive(self):
        usb = fake.FakeUnit(cylinders=40)
        result = max_track.measure(usb, 48)
        self.assertEqual(result.status, max_track.OK)
        self.assertEqual(result.max_cylinder, 39)

    def test_a_drive_the_probe_never_reaches_the_end_of(self):
        usb = fake.FakeUnit(cylinders=90)
        result = max_track.measure(usb, 48)
        self.assertTrue(result.saturated)
        self.assertEqual(result.max_cylinder, 48)

    def test_step_loss_inflates_a_single_measurement(self):
        # The pathology which made this probe report a cylinder too many.
        # A drive stopping at 39, probed at 42, slips three steps and hands
        # back a travel of 42 -- indistinguishable from having arrived.
        usb = fake.FakeUnit(cylinders=40, step_loss_period=4)
        result = max_track.measure(usb, 42)
        self.assertEqual(result.max_cylinder, 42)
        self.assertTrue(result.saturated)

    def test_the_full_search_defeats_step_loss(self):
        # Four consecutive probes cover every residue, so the minimum lands
        # on the truth however the slipping falls.
        for period in (2, 4):
            usb = fake.FakeUnit(cylinders=40, step_loss_period=period)
            result = max_track.search(usb, 60, report=lambda line: None)
            self.assertEqual(result.max_cylinder, 39,
                             'loss period %d' % period)
            self.assertEqual(result.cylinders, 40)

    def test_a_firmware_limit_is_not_a_drive_limit(self):
        usb = fake.FakeUnit(cylinders=90, firmware_limit=50)
        result = max_track.measure(usb, 60)
        self.assertEqual(result.status, max_track.FW_LIMIT)

    def test_the_search_starts_low_enough_for_a_small_drive(self):
        # A 37-cylinder drive must not be slammed into its stop by the first
        # pass. Nothing may go more than one escalation past the stop.
        usb = fake.FakeUnit(cylinders=37)
        max_track.search(usb, 60, report=lambda line: None)
        furthest = max(cylinder for cylinder, _ in usb.seeks)
        self.assertLessEqual(furthest - 36, max_track.ESCALATE_STEP * 2)


class TestNonInteractive(unittest.TestCase):
    """--non-interactive declines; --yes approves. They are opposites."""

    def test_declining_leaves_a_destructive_probe_unrun(self):
        probe = fake.StubProbe('writes', destructive=True)
        ctx = context(confirm=lambda p: False)
        core.run_all(ctx, [probe])
        self.assertFalse(probe.ran)
        self.assertEqual(ctx.results['writes'].status, 'skipped')

    def test_the_skip_is_recorded_rather_than_omitted(self):
        # A profile must be able to tell "nobody approved this" from
        # "this failed".
        probe = fake.StubProbe('writes', destructive=True)
        ctx = context(confirm=lambda p: False)
        core.run_all(ctx, [probe])
        self.assertIn('approved', ctx.results['writes'].as_dict()['reason'])

    def test_nothing_waits_when_there_is_nobody_to_wait_for(self):
        ctx = context(pause=None)
        core.run_all(ctx, [fake.StubProbe('a', needs_media=core.MEDIA_ANY)])
        self.assertIn('Load ANY disk', ctx.report.text)


if __name__ == '__main__':
    unittest.main()

# Local variables:
# python-indent: 4
# End:
