#!/usr/bin/env python3
"""Build a per-member open boundary ensemble by lagging the boundary in time.

    tools/obc-lagged.py <domain> --lags=<days,...> --out <dir>
    tools/obc-lagged.py gom_25km --lags=-21,-13,-4,4,13,21 \
        --out /data/ackbar/spike/obc-lag21/obc

`--lags=` with the equals sign, not a space: the list starts with a minus and
argparse reads that as an option rather than a value.

Writes `<dir>/mem000.nc` (the unperturbed boundary, for the control) and
`mem001..memN.nc`, one per lag, each a drop-in replacement for the domain's
`obc.nc`. Nothing is written into the domain's own `INPUT/`: a member boundary
that landed there would become every later run's boundary without saying so.

---------------------------------------------------------------------------
Why the boundary is a spread source at all

An ensemble filter removes spread every cycle. A regional ensemble whose members
share one boundary has, at the boundary, no spread at all to remove or replace,
and the interior spread it does have drains out through an edge that every member
agrees about. `docs/design.md` compares the spread sources on day 5 surface
temperature, which is the horizon that suits the other three and badly
understates this one; what the others do not reach on any horizon is sea surface
height with structure, which is the field the altimeters see and the field a Loop
Current lives in.

Counillon and Bertino (2009, Ocean Dynamics 59:83-95) measured all three sources
on this basin at 1/25 degree. Domain root-mean-square sea surface height spread
at day 35: 4.3 cm from the initial state, **2.8 cm from a lagged boundary**, and
1.8 cm from perturbed atmospheric forcing. They lagged by a random offset in
+/-37 days, citing Abascal's 20 to 40 day band in observed Yucatan transport, and
kept the range short of a season so the perturbation does not become a seasonal
bias. Their anomalies appear first at the boundary the inflow crosses, propagate
around the Loop Current at about 30 km/day, and take roughly three weeks to
mature. That last number is the one that decides an experiment's length: a 30 day
run that starts its perturbed boundary at cycle one spends most of itself
watching the perturbation grow.

---------------------------------------------------------------------------
What each member actually gets

    member(t) = base(t) + amplitude * ( base(t + lag) - mean_over_members )

not simply `base(t + lag)`. Three things follow from writing it this way, and
each is the reason for a knob that otherwise would not exist.

**The ensemble mean boundary is exactly the unperturbed boundary**, by
construction, for any number of members and any set of lags. A plain lagged
ensemble does not have that property: the mean of N lagged copies is a running
mean of the boundary, which is smoother than the boundary. That smoothing is a
bias in the one field the ensemble mean is supposed to be unbiased in, and it
does not shrink with more members, it grows with the span.

**`--amplitude` separates structure from size.** The lag supplies the structure:
a real ocean's boundary variability, already balanced, at the scales the ocean
puts it at. The scalar sets how much of it the ensemble claims. That is the same
split `ensemble.stochastic` has between the pattern and its amplitude, and it
exists for the same reason: the amplitude is the thing that has to be tuned
against measured spread, and re-tuning by re-picking lags would change the
structure at the same time and confound the two.

**Amplitude 1 is not quite the pure lagged field.** It is the lagged field plus
whatever the base has that the member mean does not, which is a mild
amplification of the sub-span variability. The alternative, dropping the `base(t)`
term, gives the pure lagged fields and gives up the exact centring. The centring
is worth more.

Lags are whole days because the boundary source is daily: an integer lag is a
slice of the file and nothing is interpolated, so a member's boundary is exactly
a real analysis and not a blend of two.

---------------------------------------------------------------------------
The window shrinks by the largest lag, at both ends

A member lagged by +L reads the base file L days beyond the last time it is asked
for. So the output covers `[first + max lag, last - max lag]` of the input, the
same window for every member, and the tool says what it cut. The window has to
cover the whole experiment including the tail of its longest extended forecast,
and MOM6 fails inside `time_interp_external` at the first timestep past the end
rather than at startup, which is a long way into a submitted graph to find out.
Fetch a longer boundary rather than shorten the span.

---------------------------------------------------------------------------
Any domain, and domains with no boundary at all

Segments, fields, levels and geometry are read out of `obc.nc` itself, so this
works on any domain that has one and knows nothing about the Gulf. A global
domain has no open boundary, no `obc.nc`, and nothing here to run; it is told so
rather than left to fail later. On a global domain the equivalent lever is
ensemble atmospheric forcing, which is a different tool.

Nothing outside `obc.nc` is read at all, not even the domain's MOM6 parameter
files, so there is no second opinion here about what the domain's boundary is.

---------------------------------------------------------------------------
Net transport is checked in the model, not here

Counillon and Bertino report a burst of spread on the first day from the
barotropic adjustment to a changed net boundary transport, damped within a day.
It is worth knowing whether these members carry one, and this is the wrong place
to compute it: `obc.nc` is flood-filled across land, so a segment integral picks
up flux through the coast, and it carries the source's levels to the sea floor of
the deepest column, so it picks up transport below the shelf. Both need the
domain's mask and topography, which is a second implementation of geometry MOM6
already has.

The observable a net imbalance produces is basin mean sea surface height drifting
apart member by member, and the spike writes SSH daily. Measure it there.

---------------------------------------------------------------------------
The basin-wide mode, and the thing that does not fix it

About 47% of the interior sea surface height spread this produces on `gom_25km`
is one basin-wide number rather than spatial structure, 3.0 cm of 4.35 cm at day
30. That matters more than its size suggests, because it is the one direction
altimetry cannot see: `ufo::ObsADT::simulateObs` computes
`offset = mean(H(x) - y)` over the observation space and subtracts it from
`H(x)`, and `ObsADTTLAD` does the same in the tangent linear and the adjoint. So
a member whose sea level is uniformly high carries an error no observation
reports and neither an LETKF nor a variational solver can remove, and it inflates
every spread-versus-error diagnostic with spread the increment cannot reach.

**Removing the boundary's own basin-wide sea surface height does not fix it, and
this was measured rather than assumed.** The reasoning that says it should is
easy to reconstruct and wrong, so it is written down here to stop it being
reconstructed: all three GoM segments are FLATHER, a FLATHER segment imposes a
prescribed head on the interior, so a boundary-wide `zeta` anomaly should pump
the basin. Two spikes were run, identical but for a generator that subtracted
each member's boundary-wide `zeta` before writing. The interior basin-wide spread
did not fall: 0.0298 m with it, 0.0362 m without, which is inside the sampling
error of seven members and certainly not the collapse the argument predicts. The
structured part was unchanged at 0.0317 m either way, and temperature and
salinity spread moved by under 2%.

The direct test says why. Correlating each member's boundary-wide `zeta` anomaly
against its day 30 basin-mean sea surface height gives **-0.23**: no relationship,
and the wrong sign. Whatever raises and lowers this basin, it is not the head
prescribed at the edge.

The remaining candidate, and the one worth building if this is ever worth fixing,
is the **net volume flux**: the perturbed normal velocities carry a different
transport through the segments, and a mass imbalance is exactly a basin that
fills or empties. Constraining each member to zero anomalous net transport needs
the domain's mask and topography, which is why it is not done here, and the same
reason is written out under "the mass check" above. Subtracting the `zeta` mode
alone would also break the source's own consistency between sea surface height
and normal velocity, which a lagged pair inherits already balanced.

So the generator leaves both alone, and the basin-wide mode is a known,
unobservable component of this scheme rather than a bug in it.
"""

import argparse
import os
import re
import sys
from pathlib import Path

import numpy as np
import netCDF4 as nc

#: Segment variables that are not themselves boundary data. `lon`/`lat` locate
#: the segment, `nz` is a vertical axis, and `dz_<field>` is the source column's
#: thicknesses, which MOM6 requires beside each field and at the field's shape.
NOT_DATA = ("lon", "lat", "nz")

def field_order(segments):
    """Every field any segment carries, in a stable order for the summary."""
    return sorted({field for fields in segments.values() for field in fields})


def ladder(span, members):
    """Member lags: magnitudes evenly spaced over the span, signs alternating.

        span 21, 6 members  ->  +7, -7, +14, -14, +21, -21

    **Alternating rather than ascending, so that a truncation is still centred.**
    `docs/design.md` states that a missing or diverged member is the normal case,
    and the mean subtraction here happens when the files are written, not when
    the filter reads them. So the set the filter actually sees is the survivors,
    and an ascending ladder truncated anywhere is an ensemble systematically
    lagged one way. Alternating means losing any even number of trailing members
    leaves the set exactly centred, and losing an odd number leaves it off by one
    spacing rather than by half a span.

    **The span is the knob, not the spacing**, because the span is what is
    physically bounded: a lag long enough to reach into another season stops
    being a mesoscale perturbation and becomes a seasonal bias. Counillon and
    Bertino keep theirs inside 37 days for exactly that reason. Spacing is
    bounded by nothing, so an ensemble parameterised by it walks out of the
    season as soon as it grows, and adding members would be claiming more
    uncertainty rather than sampling the same uncertainty better.

    The consequence to know about is that this caps the *rank* of the
    perturbation, not just its size. Members closer together than the boundary's
    decorrelation time are near duplicates, so the independent dimension is about
    `2 * span / decorrelation`, whatever the member count. On this basin the
    boundary decorrelates over 10 to 30 days, which is most of the span.
    """
    magnitudes = [span * (k + 1) / ((members + 1) // 2)
                  for k in range((members + 1) // 2)]
    lags = [int(round(sign * value))
            for value in magnitudes for sign in (1, -1)][:members]

    # Rounding to whole days collapses the ladder when the span cannot carry the
    # member count: span 5 with 12 members gives a repeated 2 and a repeated -2,
    # and span 1 with 6 gives two members lagged by nothing at all. Both are two
    # bit-identical forecasts costing two forecasts and lowering the rank, and
    # nothing downstream can tell them from an ensemble that happened to be
    # degenerate. The requested spacing is the thing to state, since it is what
    # the caller has to change.
    duplicates = len(lags) - len(set(lags))
    if duplicates:
        die(f"span {span} over {members} members needs a spacing of "
            f"{span / ((members + 1) // 2):.2f} days, which rounds to "
            f"{duplicates + 1} identical lag(s) in {lags}. Widen the span or "
            f"cut the members: whole days are the resolution a daily boundary "
            f"has.")
    return lags


def die(message):
    sys.exit(f"obc-lagged: {message}")


def domain_input(domain):
    root = os.environ.get("ACKBAR_STATIC_ROOT")
    if not root:
        die("run `source site/activate.sh` first")
    path = Path(root, "domain", domain, "INPUT")
    if not path.is_dir():
        die(f"{path} does not exist, so {domain} is not a staged domain")
    return path


def segments_of(dataset):
    """`{name: {field: variable}}` for every segment the file carries.

    Discovered from the file rather than declared, so a domain with one open
    segment and a domain with six are the same case here.

    The segment names come from the `nx_segment_<name>` dimensions and not from
    the variables, because a variable name alone is ambiguous: `nz_segment_001_temp`
    is the vertical axis of one field of segment 001, and read as a name it
    invents a segment called `001_temp`. Anchoring the match on a known name at
    the end of the variable cannot make that mistake.
    """
    names = sorted(match.group(1) for match in
                   (re.fullmatch(r"nx_segment_(\w+)", dimension)
                    for dimension in dataset.dimensions) if match)
    found = {}
    for segment in names:
        fields = {}
        for name in dataset.variables:
            match = re.fullmatch(rf"(\w+)_segment_{re.escape(segment)}", name)
            if not match:
                continue
            field = match.group(1)
            if field in NOT_DATA or field.startswith("dz_"):
                continue
            fields[field] = name
        if fields:
            found[segment] = fields
    return found


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("domain")
    ap.add_argument("--span", type=int,
                    help="largest lag in days. With --members, derives the "
                         "ladder; see `ladder`. This is the form an experiment "
                         "config carries, because it is two numbers a member "
                         "count can change without re-deciding anything.")
    ap.add_argument("--members", type=int,
                    help="how many perturbed members the ladder holds, not "
                         "counting mem000")
    ap.add_argument("--lags",
                    help="the ladder stated outright instead: comma separated "
                         "whole days, one per member, in member order. Written "
                         "with an equals sign (--lags=-21,...) because the list "
                         "starts with a minus and argparse reads that as an "
                         "option. The resolved list is always printed, whichever "
                         "form was used, because the list is the authority and "
                         "the span is a way of generating one.")
    ap.add_argument("--out", type=Path, required=True,
                    help="directory for mem000.nc onwards. Required, and never "
                         "the domain's own INPUT: a member boundary left there "
                         "becomes every later run's boundary silently.")
    ap.add_argument("--amplitude", type=float, default=1.0,
                    help="scales the anomaly about the member mean. 1.0 is the "
                         "lagged ocean's own amplitude (default)")
    ap.add_argument("--source", type=Path,
                    help="the boundary file to lag, if not the domain's obc.nc")
    args = ap.parse_args()

    source = args.source or (domain_input(args.domain) / "obc.nc")
    if not source.exists():
        die(f"{source} does not exist. A domain with no open boundary has no "
            f"boundary to perturb; on a global domain the spread source is "
            f"ensemble atmospheric forcing instead.")

    if bool(args.lags) == bool(args.span or args.members):
        die("give either --span with --members, or --lags, and not both")
    if args.lags:
        try:
            lags = [int(part) for part in args.lags.split(",") if part.strip()]
        except ValueError:
            die(f"--lags takes whole days, not {args.lags!r}. The boundary "
                f"source is daily, so a fractional lag would blend two analyses "
                f"into a boundary no ocean ever had.")
    else:
        if not (args.span and args.members):
            die("--span needs --members and the other way round")
        lags = ladder(args.span, args.members)
    if len(lags) < 2:
        die("two lags at least, or there is no ensemble")
    print(f"obc-lagged: lags {','.join(str(lag) for lag in lags)}, "
          f"amplitude {args.amplitude}")

    with nc.Dataset(source) as f:
        segments = segments_of(f)
        if not segments:
            die(f"{source} carries no <field>_segment_<name> variables, so it "
                f"is not a boundary file MOM6 would read")

        times = np.asarray(f["time"][:])
        span = max(abs(lag) for lag in lags)
        # Whole day lags against a time axis in days: the shift is an index
        # shift, and the axis has to be the day counts it claims to be for that
        # to hold. Anything else is a boundary file this tool did not write.
        # The units are checked and not only the spacing. A file written in
        # "hours since" with hourly records has steps of exactly 1.0 as well,
        # and would pass a spacing test while turning every lag in days into a
        # lag in hours: a 21 day ladder delivered as a 21 hour one, which is a
        # working ensemble with a twentyfold error in its only parameter.
        units = f["time"].units
        if not units.strip().lower().startswith("days since"):
            die(f"{source} has time units {units!r}, and this shifts records by "
                f"whole days. A lag would be that many records of whatever this "
                f"axis counts, not that many days.")
        steps = np.diff(times)
        if len(steps) and not np.allclose(steps, 1.0):
            die(f"{source} has a time axis that is not one day per record "
                f"(steps {steps.min():g} to {steps.max():g}), so a lag in days "
                f"is not a shift in records")
        if 2 * span >= len(times):
            die(f"a lag of {span} days either side leaves nothing of a "
                f"{len(times)} record file")

        keep = slice(span, len(times) - span)
        print(f"obc-lagged: {source}")
        print(f"obc-lagged: {len(times)} records, keeping "
              f"{keep.stop - keep.start} after cutting {span} days off each end")
        print(f"obc-lagged: window is {times[keep][0]:g} to {times[keep][-1]:g} "
              f"of '{units}'")
        print(f"obc-lagged: {len(segments)} segment(s): "
              + ", ".join(f"{name} ({'/'.join(sorted(fields))})"
                          for name, fields in segments.items()))

        # Every member's slice of every field, held at once: the member mean is
        # subtracted from each, so no member can be written before all of them
        # exist. A season of one domain's boundary is tens of megabytes.
        members = []
        for lag in lags:
            values = {}
            for segment, fields in segments.items():
                values[segment] = {
                    field: np.asarray(f[name][keep.start + lag:keep.stop + lag])
                    for field, name in fields.items()}
            members.append(values)

        base = {segment: {field: np.asarray(f[name][keep])
                          for field, name in fields.items()}
                for segment, fields in segments.items()}

        for segment, fields in segments.items():
            for field in fields:
                mean = sum(m[segment][field] for m in members) / len(members)
                for m in members:
                    m[segment][field] = (base[segment][field]
                                         + args.amplitude
                                         * (m[segment][field] - mean))

        args.out.mkdir(parents=True, exist_ok=True)
        written = [("mem000", 0, base)]
        written += [(f"mem{i + 1:03d}", lag, values)
                    for i, (lag, values) in enumerate(zip(lags, members))]
        print()
        print(f"{'member':8s}{'lag':>6s}   perturbation rms, over the window")
        print(f"{'':8s}{'days':>6s}"
              + "".join(f"{field:>10s}" for field in field_order(segments)))
        for name, lag, values in written:
            write_member(f, args.out / f"{name}.nc", keep, values,
                         name, lag, args.amplitude)
            row = []
            for field in field_order(segments):
                difference = np.concatenate(
                    [(values[segment][field] - base[segment][field]).ravel()
                     for segment in segments if field in segments[segment]])
                row.append(np.sqrt((difference ** 2).mean()))
            print(f"{name:8s}{lag:6d}" + "".join(f"{v:10.4f}" for v in row))
        print("Departure from the unperturbed boundary, over every segment point, "
              "level and day. mem000 is that boundary, so its row is zero.")

        # A member this run did not write is a member from a previous one, and
        # every consumer addresses these files by name: `spike-obc.sh` globs
        # `mem*.nc`, and `ensemble.inputs` resolves `mem007.nc` and checks only
        # that it exists. So a shorter ladder written over a longer one leaves an
        # ensemble whose tail is the old ladder, and whose mean is therefore no
        # longer `mem000`, which is the single property the anomaly form exists
        # to provide. Nothing downstream can detect it: with the same span the
        # record counts match too.
        expected = {f"{name}.nc" for name, _, _ in written}
        stale = sorted(p for p in args.out.glob("mem*.nc") if p.name not in expected)
        for path in stale:
            path.unlink()
        if stale:
            print(f"\nRemoved {len(stale)} member(s) left by an earlier run with "
                  f"a longer ladder: " + ", ".join(p.name for p in stale))

    print(f"\nobc-lagged: wrote {len(written)} files to {args.out}")


def write_member(source, path, keep, values, member, lag, amplitude):
    """One member, structurally identical to the file it came from.

    Everything not a segment data field is copied through: `dz_`, `lon`, `lat`,
    the time axis and its attributes. MOM6 reads `<var>_segment_00N` and
    `dz_<var>_segment_00N` and dies if their shapes disagree, so the safe
    edit is the values of one and nothing else.
    """
    perturbed = {variable: values[segment][field]
                 for segment, fields in segments_of(source).items()
                 for field, variable in fields.items()}

    # Built from the fields rather than formatted: a NOLEAP axis comes back as a
    # cftime object, which implements neither `__format__` nor `isoformat`.
    time = source["time"]
    calendar = getattr(time, "calendar", "standard")
    covered = [
        "{0.year:04d}-{0.month:02d}-{0.day:02d}T{0.hour:02d}:{0.minute:02d}:"
        "{0.second:02d}Z".format(nc.num2date(value, time.units, calendar))
        for value in (time[keep][0], time[keep][-1])]

    partial = path.with_suffix(".nc.partial")
    with nc.Dataset(partial, "w", format=source.data_model) as out:
        # The source's own globals first, so `source` still says which GLORYS
        # pull the water came from; then this tool's, because a member file
        # otherwise cannot say what was done to it. A run staged from an archive
        # is only reproducible if the archive records its own ladder: the
        # directory name carries it by convention and conventions drift.
        out.setncatts({key: source.getncattr(key) for key in source.ncattrs()})
        out.setncatts({
            "perturbation": (
                f"lagged open boundary, {member}, lag {lag:+d} days, "
                f"amplitude {amplitude}"),
            "perturbation_form": (
                "member(t) = base(t) + amplitude * (base(t + lag) - mean over "
                "members); mem000 is base and its lag is 0"),
            # ACDD names, and they earn their place: a ladder trims max|lag| off
            # each end, so a member covers a shorter window than the file it came
            # from and than `source` names. An experiment cycling past the end
            # gets `time_interp_external ... is before/after range of list` from
            # inside MOM6, minutes into a job, from whichever PE owns a segment.
            # These two attributes are what makes that answerable from the file.
            "time_coverage_start": covered[0],
            "time_coverage_end": covered[-1],
        })
        for name, dimension in source.dimensions.items():
            out.createDimension(name, None if dimension.isunlimited()
                                else len(dimension))
        for name, variable in source.variables.items():
            # `_FillValue` is settable only at creation time, and on a
            # NETCDF4_CLASSIC file setting it afterwards is a hard error rather
            # than a no-op. The files this tool writes carry none, so this is
            # only reachable through `--source`, which is exactly the case where
            # the file came from somewhere else.
            attributes = {key: variable.getncattr(key)
                          for key in variable.ncattrs()}
            fill = attributes.pop("_FillValue", None)
            copy = out.createVariable(name, variable.datatype, variable.dimensions,
                                      **({} if fill is None else {"fill_value": fill}))
            copy.setncatts(attributes)
            if name in perturbed:
                copy[:] = perturbed[name]
            elif "time" in variable.dimensions:
                copy[:] = variable[keep]
            else:
                copy[:] = variable[:]
    partial.rename(path)


if __name__ == "__main__":
    main()
