# greaseweazle/tools/probe/core.py
#
# The probe contract, and the orchestrator which runs probes to it.
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

# Every probe is a self-contained module which declares what it needs and
# owns its own presentation. Nothing here knows about any particular probe,
# so adding one means writing its module and naming it in the registry --
# not editing the orchestrator in a dozen places.
#
# A probe module provides:
#
#     name         short identifier, used for selection and in the profile
#     title        heading for its section of the report
#     summary      one line, for --list-probes
#     depends_on   names of probes whose results qualify this one
#     destructive  True if it writes to the disk
#     needs_motor  True if it needs the spindle turning
#     wears_drive  True if running it measurably wears the mechanism
#     run(ctx)     perform the measurement, returning a Result
#
# and its Result satisfies the Result protocol below.

from typing import (Any, Callable, Dict, Iterable, List, NamedTuple,
                    Optional, Protocol, Sequence)

from greaseweazle import error
from greaseweazle import usb as USB


class Result(Protocol):
    '''What every probe returns.

    A Protocol rather than a base class: each probe's Result carries quite
    different fields and they are all NamedTuples, so what matters is the
    shared surface, not a shared ancestor.
    '''

    # Read-only: every Result is a NamedTuple, whose fields cannot be
    # assigned. Declaring a plain attribute here would demand a settable one
    # and reject all of them.
    @property
    def status(self) -> str:
        '''Machine-readable outcome, recorded in the profile.'''
        ...

    @property
    def ok(self) -> bool:
        '''True if probes depending on this one may believe it.'''
        ...

    def as_dict(self) -> Dict[str, Any]:
        '''JSON-friendly form, for the drive profile.'''
        ...

    def report(self, out: Callable[[str], None]) -> None:
        '''Present this result to the user.'''
        ...


class Probe(Protocol):
    name: str
    title: str
    summary: str
    depends_on: Sequence[str]
    destructive: bool
    needs_motor: bool
    wears_drive: bool

    def run(self, ctx: 'Context') -> Result:
        ...


class Skipped(NamedTuple):
    '''Stands in for a probe which did not run.

    Recorded rather than simply omitted, because "not measured" and "measured
    and failed" mean entirely different things when two drive profiles are
    compared: an absent field must never read as a change.
    '''

    reason: str
    status: str = 'skipped'

    @property
    def ok(self) -> bool:
        return False

    def as_dict(self) -> Dict[str, Any]:
        return {'status': self.status, 'reason': self.reason}

    def report(self, out: Callable[[str], None]) -> None:
        out('  Not run.')
        out('  (%s)' % self.reason)


class Context:
    '''What a probe is given: the drive, the options, and what ran before.'''

    def __init__(self, usb: USB.Unit, options: Any,
                 confirm: Callable[[Probe], bool],
                 out: Callable[[str], None] = print) -> None:
        self.usb = usb
        self.options = options
        self._confirm = confirm
        self.report = out
        self.results: Dict[str, Result] = {}

    def record(self, probe: Probe, result: Result) -> None:
        self.results[probe.name] = result

    def result(self, probe: Any) -> Any:
        '''Result of an earlier probe, by module.

        Keyed by the module rather than by its name so that the caller keeps
        the concrete Result type: a probe reading another's measurement wants
        the field, not an opaque object.
        '''
        return self.results.get(probe.name)

    def ok(self, name: str) -> bool:
        result = self.results.get(name)
        return result is not None and result.ok

    def confirm(self, probe: Probe) -> bool:
        return self._confirm(probe)

    def as_dict(self) -> Dict[str, Any]:
        return dict((name, result.as_dict())
                    for name, result in self.results.items())


def ordered(probes: Iterable[Probe]) -> List[Probe]:
    '''Probes in an order which satisfies their dependencies.

    Sorted from the declarations rather than maintained by hand, so a probe
    cannot be added in the wrong place, and a cycle is reported instead of
    silently producing an order that cannot be run.

    Orders whatever it is given. A dependency outside the set is not an
    error here: it means that probe was not selected, and run_all will skip
    whatever needed it, saying so. Registry validity is checked by select().
    '''
    probes = list(probes)
    by_name = dict((p.name, p) for p in probes)
    state: Dict[str, str] = {}
    result: List[Probe] = []

    def visit(probe: Probe) -> None:
        seen = state.get(probe.name)
        if seen == 'done':
            return
        error.check(seen != 'visiting',
                    'Probe dependency cycle involving %s' % probe.name)
        state[probe.name] = 'visiting'
        for dependency in probe.depends_on:
            if dependency in by_name:
                visit(by_name[dependency])
        state[probe.name] = 'done'
        result.append(probe)

    for probe in probes:
        visit(probe)
    return result


def select(probes: Sequence[Probe], only: Optional[List[str]],
           destructive: bool = False,
           allow_wear: bool = False) -> List[Probe]:
    '''Which probes to run, in order.

    'only' names the probes explicitly asked for, or None for all of them.
    Prerequisites are added automatically: running a probe without whatever
    qualifies its result would produce a number nobody should trust.

    A probe which wears the mechanism is never added on anyone's behalf --
    not by a plain run, and not as somebody else's prerequisite. It runs when
    it is named or when wear is allowed outright, and otherwise whatever
    depended on it is skipped with the reason given. Pulling a wearing probe
    in transitively is exactly the surprise this exists to prevent.
    '''
    by_name = dict((p.name, p) for p in probes)

    for probe in probes:
        for dependency in probe.depends_on:
            error.check(dependency in by_name,
                        'Probe %s depends on unknown probe %s'
                        % (probe.name, dependency))

    if only is None:
        named: Sequence[str] = ()
        chosen = set(p.name for p in probes
                     # Destructive probes are opt-in, never part of a plain
                     # run, however convenient it would be to include them.
                     if destructive or not p.destructive)
    else:
        unknown = [name for name in only if name not in by_name]
        error.check(not unknown,
                    'Unknown probe(s): %s\nAvailable: %s'
                    % (', '.join(unknown),
                       ', '.join(p.name for p in probes)))
        named = only
        chosen = set(only)
        pending = list(chosen)
        while pending:
            for dependency in by_name[pending.pop()].depends_on:
                if dependency not in chosen:
                    chosen.add(dependency)
                    pending.append(dependency)

    if not allow_wear:
        chosen = set(name for name in chosen
                     if not by_name[name].wears_drive or name in named)

    return ordered([by_name[name] for name in chosen])


def needs_motor(probes: Iterable[Probe]) -> bool:
    '''True if any of these probes needs the spindle turning.

    Asked before the drive is selected, since the motor is switched on for
    the whole session rather than per probe.
    '''
    return any(p.needs_motor for p in probes)


def run_all(ctx: Context, probes: Sequence[Probe]) -> None:
    '''Run each probe, skipping any whose prerequisites did not hold.'''

    for probe in probes:
        unmet = [name for name in probe.depends_on if not ctx.ok(name)]
        if unmet:
            result: Result = Skipped(
                'depends on %s, which did not produce a usable result'
                % ', '.join(unmet))
        elif probe.destructive and not ctx.confirm(probe):
            # Consent is enforced here, once, rather than trusted to each
            # destructive probe to remember.
            result = Skipped('not approved')
        else:
            result = probe.run(ctx)

        ctx.record(probe, result)
        ctx.report('')
        ctx.report('%s:' % probe.title)
        result.report(ctx.report)

# Local variables:
# python-indent: 4
# End:
