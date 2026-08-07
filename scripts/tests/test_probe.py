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
from greaseweazle.tools import probe
from greaseweazle.tools.probe import (
    consent, core, index_sensor, max_track, max_track_write, spin_up, trk0)


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
            cylinder = max_track.START_CYLINDER
            while cylinder <= stop:
                cylinder += max_track.ESCALATE_STEP
            self.assertLessEqual(cylinder - stop, max_track.ESCALATE_STEP,
                                 'overshot on a %d-cylinder drive' % stop)


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

    def test_agreement_with_the_step_counting_method(self):
        # Two methods with quite different failure modes reaching the same
        # answer is the whole value of this probe, so it is recorded rather
        # than left for the reader to compare by eye.
        found = max_track_write.interpret([(79, 79), (80, 80), (83, 91)])
        self.assertTrue(found._replace(stepping_answer=83).agrees)
        self.assertFalse(found._replace(stepping_answer=84).agrees)

    def test_agreement_is_unknown_without_both_answers(self):
        found = max_track_write.interpret([(79, 79), (80, 80), (83, 91)])
        self.assertIsNone(found.agrees)
        inconclusive = max_track_write.interpret([(79, 79), (80, 80)])
        self.assertIsNone(inconclusive._replace(stepping_answer=83).agrees)


def trk0_walk(walk_to=4, home=True, away=(), returned=True):
    """Build a /TRK0 walk. 'away' lists cylinders wrongly asserting."""
    outward = [(0, home)] + [(c, c in away) for c in range(1, walk_to + 1)]
    homeward = ([(c, c in away) for c in range(walk_to - 1, 0, -1)]
                + [(0, returned)])
    return outward, homeward


class TestTrk0(unittest.TestCase):
    """Every fault here is synthetic: a drive with a dead Track 0 sensor is
    not something that can be sourced on demand, so the readings are built
    rather than recorded."""

    def test_healthy_sensor(self):
        result = trk0.interpret(*trk0_walk())
        self.assertEqual(result.status, trk0.OK)
        self.assertTrue(result.ok)

    def test_stuck_asserted(self):
        # Asserted everywhere. This is the fault that would otherwise look
        # like a head which never moves.
        result = trk0.interpret(*trk0_walk(away=(1, 2, 3, 4)))
        self.assertEqual(result.status, trk0.STUCK_ASSERTED)
        self.assertFalse(result.ok)

    def test_no_signal_at_home(self):
        result = trk0.interpret(*trk0_walk(home=False))
        self.assertEqual(result.status, trk0.ABSENT_AT_HOME)
        self.assertFalse(result.ok)

    def test_never_re_asserts(self):
        # usb.py documents drives which do not assert /TRK0 stepping inward,
        # so this direction-dependent fault is real, not hypothetical.
        result = trk0.interpret(*trk0_walk(returned=False))
        self.assertEqual(result.status, trk0.NO_REASSERT)
        self.assertFalse(result.ok)

    def test_intermittent(self):
        result = trk0.interpret(*trk0_walk(away=(2,)))
        self.assertEqual(result.status, trk0.INCONSISTENT)
        self.assertFalse(result.ok)

    def test_only_ok_is_trusted(self):
        # Everything downstream is gated on this, so a new status must not
        # default to being trusted.
        for status in (trk0.ABSENT_AT_HOME, trk0.STUCK_ASSERTED,
                       trk0.NO_REASSERT, trk0.INCONSISTENT):
            self.assertFalse(trk0.Result(status, '').ok, status)
        self.assertTrue(trk0.Result(trk0.OK, '').ok)

    def test_result_is_json_shaped(self):
        import json
        json.dumps(trk0.interpret(*trk0_walk()).as_dict())

    def test_rejects_a_walk_that_never_leaves_cylinder_0(self):
        with self.assertRaises(error.Fatal):
            trk0.interpret([(0, True)], [(0, True)])

    def test_rejects_a_walk_not_anchored_at_cylinder_0(self):
        with self.assertRaises(error.Fatal):
            trk0.interpret([(1, False), (2, False)], [(1, False)])


class TestProbeSelection(unittest.TestCase):
    """--only, and the dependencies it must not let you skip."""

    def test_default_run_excludes_destructive_probes(self):
        chosen = [p.name for p in core.select(probe.PROBES, None)]
        self.assertIn(trk0.name, chosen)
        self.assertIn(max_track.name, chosen)
        self.assertNotIn(max_track_write.name, chosen)

    def test_destructive_flag_opts_them_in(self):
        chosen = [p.name for p in
                  core.select(probe.PROBES, None, destructive=True)]
        self.assertIn(max_track_write.name, chosen)

    def test_selecting_one_probe_runs_only_it(self):
        chosen = [p.name for p in core.select(probe.PROBES, [trk0.name])]
        self.assertEqual(chosen, [trk0.name])

    def test_dependencies_are_pulled_in(self):
        # max-track measures against /TRK0, so asking for it alone must
        # still validate the sensor, or the figure is unqualified.
        chosen = [p.name for p in core.select(probe.PROBES, [max_track.name])]
        self.assertEqual(chosen, [trk0.name, max_track.name])

    def test_transitive_dependencies_are_pulled_in(self):
        chosen = [p.name for p in
                  core.select(probe.PROBES, [max_track_write.name])]
        self.assertEqual(chosen,
                         [trk0.name, max_track.name, max_track_write.name])

    def test_result_is_always_in_dependency_order(self):
        # Declared order, not the order the user happened to type.
        chosen = [p.name for p in
                  core.select(probe.PROBES, [max_track.name, trk0.name])]
        self.assertEqual(chosen, [trk0.name, max_track.name])

    def test_unknown_probe_is_rejected(self):
        with self.assertRaises(error.Fatal):
            core.select(probe.PROBES, ['no-such-probe'])

    def test_every_registered_probe_meets_the_contract(self):
        # A probe missing any of these is unselectable, unreportable, or
        # silently exempt from the consent gate.
        for p in probe.PROBES:
            for attr in ('name', 'title', 'summary',
                         'depends_on', 'destructive', 'run'):
                self.assertTrue(hasattr(p, attr),
                                '%s lacks %s' % (p, attr))

    def test_dependencies_name_registered_probes(self):
        names = set(p.name for p in probe.PROBES)
        for p in probe.PROBES:
            for dependency in p.depends_on:
                self.assertIn(dependency, names,
                              '%s depends on unregistered %s'
                              % (p.name, dependency))


def spin(revolutions, period=0.1669, jitter=0.0):
    """Even index gaps, optionally with a little wobble."""
    return [period + (jitter if n % 2 else -jitter)
            for n in range(revolutions)]


class TestIndexSensor(unittest.TestCase):
    """Faults here are synthetic: an intermittent index sensor cannot be
    ordered on demand, and a healthy drive will not produce one to order."""

    def test_healthy_signal(self):
        result = index_sensor.interpret(spin(8))
        self.assertEqual(result.status, index_sensor.OK)
        self.assertTrue(result.ok)

    def test_reports_measured_speed_without_judging_it(self):
        # 166.9ms is 359 rpm; 200ms is 300 rpm. Both are healthy, because
        # nothing here knows what the drive is supposed to do.
        for period in (0.1669, 0.2, 0.5):
            result = index_sensor.interpret(spin(8, period=period))
            self.assertEqual(result.status, index_sensor.OK)
            self.assertAlmostEqual(result.rpm, 60.0 / period, places=3)

    def test_no_pulses_and_no_flux_means_the_drive_will_not_read(self):
        # Measured: an empty drive and an upside-down disk both return zero
        # transitions and zero pulses, so this must NOT claim the drive is
        # empty -- a disk in backwards reads identically. The inverted disk
        # was observed spinning; the drive gates its read output when it is
        # not ready, so "no flux" is not evidence of "not turning".
        result = index_sensor.interpret([], flux_seen=False)
        self.assertEqual(result.status, index_sensor.NOT_READABLE)
        self.assertIsNone(result.period)
        self.assertFalse(result.ok)
        # Must point at the index hole, which is the actionable cause, and
        # must not claim the disk is not turning: measured, a taped-over
        # index hole stops the flux dead while the disk spins normally.
        self.assertIn('index hole', result.detail)
        self.assertNotIn('not spinning', result.detail)

    def test_no_pulses_but_flux_points_at_the_index_hole(self):
        # Something is turning and being read, so the hole is the problem:
        # covered over, or a drive with no index sensor at all.
        result = index_sensor.interpret([], flux_seen=True)
        self.assertEqual(result.status, index_sensor.NO_INDEX_HOLE)
        self.assertFalse(result.ok)

    def test_no_pulses_with_media_unknown_stays_vague(self):
        # Better to say the cause was not established than to guess at one.
        result = index_sensor.interpret([])
        self.assertEqual(result.status, index_sensor.ABSENT)
        self.assertIsNone(result.rpm)

    def test_dropped_pulse_shows_as_a_double_length_gap(self):
        gaps = spin(8)
        gaps[3] *= 2
        result = index_sensor.interpret(gaps)
        self.assertEqual(result.status, index_sensor.DROPPED)
        self.assertFalse(result.ok)

    def test_spurious_pulse_shows_as_a_short_gap(self):
        gaps = spin(8)
        gaps[3] /= 3
        result = index_sensor.interpret(gaps)
        self.assertEqual(result.status, index_sensor.SPURIOUS)

    def test_spurious_beats_dropped_when_both_appear(self):
        # An extra pulse splits one revolution into a short gap and a long
        # one, so both signatures show; the extra pulse is the cause.
        gaps = spin(8)
        gaps[3], gaps[4] = gaps[3] / 4, gaps[4] * 1.75
        self.assertEqual(index_sensor.interpret(gaps).status,
                         index_sensor.SPURIOUS)

    def test_jittery_but_one_per_revolution(self):
        result = index_sensor.interpret(spin(8, jitter=0.005))
        self.assertEqual(result.status, index_sensor.JITTERY)
        self.assertGreater(result.jitter_pct, index_sensor.JITTER_LIMIT_PCT)

    def test_small_wobble_is_still_healthy(self):
        result = index_sensor.interpret(spin(8, jitter=0.0001))
        self.assertEqual(result.status, index_sensor.OK)

    def test_too_few_revolutions_to_judge(self):
        result = index_sensor.interpret(spin(2))
        self.assertEqual(result.status, index_sensor.TOO_FEW)
        self.assertFalse(result.ok)

    def test_readings_from_a_real_drive(self):
        # Eight gaps captured from a 5.25" drive, in ms, after discarding the
        # partial first revolution of the capture.
        gaps_ms = [166.911, 166.912, 166.912, 166.908,
                   166.912, 166.908, 166.908, 166.912]
        result = index_sensor.interpret([g / 1e3 for g in gaps_ms])
        self.assertEqual(result.status, index_sensor.OK)
        self.assertAlmostEqual(result.period * 1e3, 166.911, places=2)
        self.assertLess(result.jitter_pct, 0.01)

    def test_result_is_json_shaped(self):
        import json
        json.dumps(index_sensor.interpret(spin(8)).as_dict())
        json.dumps(index_sensor.interpret([]).as_dict())


def runup(first, period=0.1669, revolutions=10, ramp=()):
    """Index pulse times for a run-up.

    'ramp' gives any slow revolutions before the drive reaches 'period'.
    An empty ramp is the bench drive, which emits no index until it is
    already at speed.
    """
    times, t = [], first
    for slow in ramp:
        times.append(t)
        t += slow
    for _ in range(revolutions):
        times.append(t)
        t += period
    return times


class TestSpinUp(unittest.TestCase):

    def test_drive_that_delivers_no_index_until_at_speed(self):
        # The bench drive: first pulse already at the settled period, so no
        # acceleration is visible and the figure is time-to-index.
        result = spin_up.interpret(runup(0.75))
        self.assertEqual(result.status, spin_up.OK)
        self.assertFalse(result.transient_seen)
        self.assertAlmostEqual(result.first_pulse, 0.75)
        self.assertEqual(result.steady_at, result.first_pulse)

    def test_drive_that_shows_the_run_up(self):
        # A drive which does not gate its index would pulse while still
        # accelerating, and then the two figures differ.
        result = spin_up.interpret(runup(0.2, ramp=(0.40, 0.30, 0.22, 0.18)))
        self.assertEqual(result.status, spin_up.OK)
        self.assertTrue(result.transient_seen)
        self.assertAlmostEqual(result.first_pulse, 0.2)
        self.assertGreater(result.steady_at, result.first_pulse)

    def test_no_pulses_at_all(self):
        result = spin_up.interpret([])
        self.assertEqual(result.status, spin_up.NO_PULSES)
        self.assertFalse(result.ok)

    def test_too_few_revolutions(self):
        result = spin_up.interpret([0.75, 0.92, 1.09])
        self.assertEqual(result.status, spin_up.TOO_FEW)
        self.assertFalse(result.ok)

    def test_speed_that_never_settles(self):
        # Must time out and report, not hang or invent an answer.
        wandering = [0.3]
        for n in range(10):
            wandering.append(wandering[-1] + 0.16 + 0.02 * (n % 3))
        result = spin_up.interpret(wandering)
        self.assertEqual(result.status, spin_up.NEVER_STEADY)
        self.assertFalse(result.ok)

    def test_result_is_json_shaped(self):
        import json
        json.dumps(spin_up.interpret(runup(0.75)).as_dict())
        json.dumps(spin_up.interpret([]).as_dict())


class TestSpinUpReconcile(unittest.TestCase):
    """The index hole quantises this measurement by up to a revolution."""

    def test_takes_the_smallest_not_the_mean(self):
        # Measured across five runs on the bench drive. The readings fall in
        # two clusters a revolution apart; a mean would sit between them, in
        # a gap where the drive never actually became ready.
        readings = [0.9098, 0.9135, 0.9147, 0.7504, 0.7505]
        best, spread = spin_up.reconcile(readings)
        self.assertAlmostEqual(best, 0.7504)
        self.assertAlmostEqual(spread, 0.1643, places=4)

    def test_the_spread_is_about_one_revolution(self):
        # Not noise: it is where the hole happened to be. Guards the claim
        # that a comparison tolerance must exceed a whole revolution.
        _, spread = spin_up.reconcile([0.9098, 0.9135, 0.9147,
                                       0.7504, 0.7505])
        self.assertLess(spread, 0.1669)
        self.assertGreater(spread, 0.1669 * 0.9)

    def test_agreement_reports_zero_spread(self):
        best, spread = spin_up.reconcile([0.75, 0.75])
        self.assertEqual(best, 0.75)
        self.assertEqual(spread, 0.0)

    def test_no_readings_is_an_error(self):
        with self.assertRaises(error.Fatal):
            spin_up.reconcile([])


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
