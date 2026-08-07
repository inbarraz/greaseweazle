# scripts/tests/test_probe.py
#
# Unit tests for the 'gw probe' interpretation logic.
#
# These are pure: no Greaseweazle and no drive is needed. Run them with
#   python3 -m unittest discover -s scripts/tests
# The standard library's unittest is used deliberately, to keep the project
# free of test-only dependencies.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

from typing import List, Tuple
import unittest

from greaseweazle import error
from greaseweazle.tools.probe import consent, max_track, max_track_write


def stepback(probe_cylinder: int, reachable: int) -> List[Tuple[int, bool]]:
    """Simulate the observations a drive would produce.

    'reachable' is the highest cylinder the head can physically get to. The
    head stalls there while the firmware counts on to probe_cylinder, so on
    the way back /TRK0 asserts once the head, not the firmware, reaches zero.
    """
    reached = min(probe_cylinder, reachable)
    samples = []
    for cyl in range(probe_cylinder, -1, -1):
        physical = reached - (probe_cylinder - cyl)
        at_track0 = physical <= 0
        samples.append((cyl, at_track0))
        if at_track0:
            break
    return samples


class TestStepbackModel(unittest.TestCase):
    """The simulation itself, since every other test leans on it."""

    def test_stalled_head_asserts_trk0_early(self):
        # An 80-cylinder drive probed at 86 stalls 7 short, so /TRK0 comes
        # back while the firmware still thinks it is at cylinder 7.
        samples = stepback(86, 79)
        self.assertEqual(samples[0], (86, False))
        self.assertEqual(samples[-1], (7, True))
        self.assertTrue(all(not trk0 for _, trk0 in samples[:-1]))

    def test_head_that_reaches_probe_asserts_at_zero(self):
        samples = stepback(86, 86)
        self.assertEqual(samples[-1], (0, True))


class TestInterpret(unittest.TestCase):

    def test_80_cylinder_drive(self):
        result = max_track.interpret(86, stepback(86, 79))
        self.assertEqual(result.status, max_track.OK)
        self.assertEqual(result.max_cylinder, 79)
        self.assertEqual(result.cylinders, 80)
        self.assertFalse(result.saturated)

    def test_40_cylinder_drive(self):
        result = max_track.interpret(48, stepback(48, 39))
        self.assertEqual(result.status, max_track.OK)
        self.assertEqual(result.max_cylinder, 39)
        self.assertEqual(result.cylinders, 40)
        self.assertFalse(result.saturated)

    def test_37_cylinder_drive(self):
        # Some early drives have as few as 37 cylinders. Nothing about the
        # measurement may presume a round or modern number.
        result = max_track.interpret(48, stepback(48, 36))
        self.assertEqual(result.status, max_track.OK)
        self.assertEqual(result.max_cylinder, 36)
        self.assertEqual(result.cylinders, 37)


class TestSearchStartsLow(unittest.TestCase):
    """The outward search must not open near a large drive's cylinder count."""

    def test_starts_below_the_smallest_drive_we_know_of(self):
        # A 37-cylinder drive must not have its head driven into the stop by
        # the very first pass, before the search has learned anything.
        self.assertLess(max_track.START_CYLINDER, 37)

    def test_overshoot_is_bounded_by_one_escalation(self):
        # Whatever the drive, the head is never driven further past its stop
        # than a single escalation step.
        for stop in (36, 39, 43, 79, 83):
            probe = max_track.START_CYLINDER
            while probe <= stop:
                probe += max_track.ESCALATE_STEP
            self.assertLessEqual(probe - stop, max_track.ESCALATE_STEP,
                                 'overshot on a %d-cylinder drive' % stop)

    def test_drive_reaching_probe_cylinder_is_a_lower_bound(self):
        # The drive got as far as we asked, so we have not found its stop.
        result = max_track.interpret(43, stepback(43, 100))
        self.assertEqual(result.status, max_track.OK)
        self.assertEqual(result.max_cylinder, 43)
        self.assertTrue(result.saturated)

    def test_head_that_never_moved(self):
        # /TRK0 still asserted at the probe cylinder.
        result = max_track.interpret(86, stepback(86, 0))
        self.assertEqual(result.status, max_track.NO_MOVEMENT)
        self.assertIsNone(result.max_cylinder)
        self.assertIsNone(result.cylinders)
        self.assertFalse(result.saturated)

    def test_no_track0_signal_at_all(self):
        # A dead or stuck-high /TRK0 sensor: stepped all the way home
        # without it ever asserting. Not something we can source a drive
        # for, hence the synthetic case.
        samples = [(cyl, False) for cyl in range(86, -1, -1)]
        result = max_track.interpret(86, samples)
        self.assertEqual(result.status, max_track.NO_TRK0)
        self.assertIsNone(result.max_cylinder)

    def test_off_by_one_at_the_boundary(self):
        # Guards the arithmetic: stalling one cylinder short must report
        # one fewer cylinder, not the same or two fewer.
        for reachable in range(30, 40):
            result = max_track.interpret(43, stepback(43, reachable))
            self.assertEqual(result.max_cylinder, reachable)
            self.assertEqual(result.cylinders, reachable + 1)


class TestReconcile(unittest.TestCase):
    """Choosing an answer from several disagreeing measurements."""

    def test_takes_the_minimum_not_the_average(self):
        # Lost steps add travel that never happened, so the smallest
        # reading is the honest one. An average would land above the stop.
        best, spread = max_track.reconcile([(97, 85), (98, 86),
                                            (99, 87), (100, 84)])
        self.assertEqual(best, 84)
        self.assertEqual(spread, 3)

    def test_agreement_reports_zero_spread(self):
        best, spread = max_track.reconcile([(90, 79), (91, 79)])
        self.assertEqual(best, 79)
        self.assertEqual(spread, 0)

    def test_single_observation(self):
        best, spread = max_track.reconcile([(43, 39)])
        self.assertEqual(best, 39)
        self.assertEqual(spread, 0)

    def test_no_observations_is_an_error(self):
        with self.assertRaises(error.Fatal):
            max_track.reconcile([])

    def test_consecutive_readings_from_a_real_drive(self):
        # Recorded from a 5.25" drive whose stop is cylinder 83. Each reading
        # is the stop plus (overdrive mod 4) phantom steps; probe 99 had an
        # overdrive divisible by 4 and so came back clean.
        readings = [(97, 85), (98, 86), (99, 83), (100, 84)]
        best, spread = max_track.reconcile(readings)
        self.assertEqual(best, 83)
        self.assertEqual(spread, 3)

    def test_non_consecutive_readings_understate_the_inflation(self):
        # The same drive, probed at 86/88/90/100 instead. Every reading is
        # inflated, so the minimum lands one cylinder high. This is not a
        # reconcile() bug -- it cannot invent a clean reading -- which is why
        # run() probes CONSECUTIVE cylinders to cover every residue mod 4.
        readings = [(86, 86), (88, 84), (90, 86), (100, 84)]
        best, _ = max_track.reconcile(readings)
        self.assertEqual(best, 84)     # true stop is 83
        self.assertNotEqual(best, 83)

    def test_four_consecutive_probes_always_include_a_clean_one(self):
        # The property run() relies on, checked against every alignment of
        # the loss pattern rather than the one the bench drive happened to
        # present.
        stop = 83
        for lowest in range(stop + 1, stop + 20):
            readings = [(probe, stop + ((probe - stop) % 4))
                        for probe in range(lowest, lowest + 4)]
            best, _ = max_track.reconcile(readings)
            self.assertEqual(best, stop, 'failed probing %d-%d'
                             % (lowest, lowest + 3))
            self.assertTrue(all(t >= stop for _, t in readings))


class TestInterpretRejectsBadInput(unittest.TestCase):

    def test_zero_probe_cylinder(self):
        with self.assertRaises(error.Fatal):
            max_track.interpret(0, [(0, True)])

    def test_no_samples(self):
        with self.assertRaises(error.Fatal):
            max_track.interpret(43, [])

    def test_samples_not_starting_at_probe_cylinder(self):
        with self.assertRaises(error.Fatal):
            max_track.interpret(43, [(42, False), (41, True)])


class TestResultAsDict(unittest.TestCase):
    """The profile in task #13 consumes this, so it is part of the contract."""

    def test_dict_is_json_shaped(self):
        import json
        result = max_track.interpret(86, stepback(86, 79))
        d = result.as_dict()
        self.assertEqual(d['status'], 'ok')
        self.assertEqual(d['max_cylinder'], 79)
        self.assertEqual(d['cylinders'], 80)
        self.assertFalse(d['saturated'])
        json.dumps(d)  # must be serialisable

    def test_unknown_result_keeps_null_fields(self):
        result = max_track.interpret(86, stepback(86, 0))
        d = result.as_dict()
        self.assertIsNone(d['max_cylinder'])
        self.assertIsNone(d['cylinders'])


class TestMarkerCoding(unittest.TestCase):
    """Cylinder <-> flux-period marker, with no drive involved."""

    def test_round_trip(self):
        for cylinder in range(79, 92):
            us = max_track_write.marker_us(cylinder, 79)
            self.assertEqual(
                max_track_write.decode_marker(us, 79, 91), cylinder)

    def test_tolerates_realistic_jitter(self):
        # A read-back period is a median over thousands of intervals, but it
        # will not land exactly on the written value.
        us = max_track_write.marker_us(85, 79)
        for drift in (-0.15, -0.05, 0.05, 0.15):
            self.assertEqual(
                max_track_write.decode_marker(us + drift, 79, 91), 85)

    def test_the_window_run_uses_stays_within_the_readable_span(self):
        # Markers past MARKER_MAX_US did not survive the round trip on the
        # bench drive, so the window run() picks must not need them.
        span = max_track_write.WINDOW_BELOW + max_track_write.WINDOW_ABOVE
        self.assertTrue(max_track_write.window_fits(0, span))

    def test_rejects_periods_outside_the_window(self):
        self.assertIsNone(max_track_write.decode_marker(0.5, 79, 91))
        self.assertIsNone(max_track_write.decode_marker(99.0, 79, 91))

    def test_rejects_a_period_between_two_markers(self):
        # Unwritten or corrupted media must decode to None, not to whichever
        # marker happens to be nearest.
        midpoint = (max_track_write.marker_us(85, 79)
                    + max_track_write.MARKER_STEP_US / 2)
        self.assertIsNone(max_track_write.decode_marker(midpoint, 79, 91))

    def test_tolerance_leaves_a_real_rejection_band(self):
        # If tolerance reached half a step, every in-range period would
        # decode to something and unwritten media would read as a valid
        # marker. Guards that the band stays discriminating.
        self.assertLess(max_track_write.MARKER_TOLERANCE_US,
                        max_track_write.MARKER_STEP_US / 2)


class TestMarkerInterpret(unittest.TestCase):

    def test_pile_up_identifies_the_stop(self):
        # Cylinders below the stop read their own marker; the stop reads the
        # last marker written, because every write past it landed there.
        readings = [(79, 79), (80, 80), (81, 81), (82, 82), (83, 91)]
        result = max_track_write.interpret(readings)
        self.assertEqual(result.status, max_track_write.OK)
        self.assertEqual(result.max_cylinder, 83)
        self.assertEqual(result.cylinders, 84)

    def test_agrees_with_the_bench_drive(self):
        # The step-counting probe reported 84 cylinders (0-83) on the bench
        # drive; this is what the write test should independently produce.
        readings = [(79, 79), (80, 80), (81, 81), (82, 82), (83, 91)]
        self.assertEqual(max_track_write.interpret(readings).max_cylinder, 83)

    def test_no_stop_within_the_window(self):
        readings = [(79, 79), (80, 80), (81, 81)]
        result = max_track_write.interpret(readings)
        self.assertEqual(result.status, max_track_write.BEYOND_WINDOW)
        self.assertIsNone(result.max_cylinder)

    def test_mismatch_on_the_first_cylinder_is_ambiguous(self):
        # The window started at or past the stop, so the reading bounds the
        # answer but does not pin it down.
        readings = [(79, 91), (80, 91)]
        result = max_track_write.interpret(readings)
        self.assertEqual(result.status, max_track_write.AT_WINDOW_START)

    def test_undecodable_marker_is_not_treated_as_a_stop(self):
        readings = [(79, 79), (80, None), (81, 81)]
        result = max_track_write.interpret(readings)
        self.assertEqual(result.status, max_track_write.UNREADABLE)
        self.assertIsNone(result.max_cylinder)

    def test_no_readings_is_an_error(self):
        with self.assertRaises(error.Fatal):
            max_track_write.interpret([])

    def test_result_is_json_shaped(self):
        import json
        result = max_track_write.interpret([(79, 79), (80, 91)])
        json.dumps(result.as_dict())


class TestConsent(unittest.TestCase):
    """The gate in front of every destructive probe."""

    def test_requires_the_exact_word(self):
        for answer in ('yes', 'y', 'YES', '', 'no'):
            self.assertFalse(
                consent.confirm('Test', prompt=lambda _: answer),
                'accepted %r' % answer)

    def test_accepts_yes(self):
        self.assertTrue(consent.confirm('Test', prompt=lambda _: 'Yes'))

    def test_assume_yes_does_not_prompt(self):
        def refuse(_):
            raise AssertionError('should not have prompted')
        self.assertTrue(
            consent.confirm('Test', assume_yes=True, prompt=refuse))


if __name__ == '__main__':
    unittest.main()

# Local variables:
# python-indent: 4
# End:
