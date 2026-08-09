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
agrees about. `docs/design.md` measures the two sources ACKBAR implements on day
5 surface temperature; neither reaches sea surface height, which is the field the
altimeters see and the field a Loop Current lives in.

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
The head that nothing can correct

Sea surface height is the one field whose boundary-wide average is removed from
each member's perturbation before it is written, and this is not a tuning choice.

`ufo::ObsADT::simulateObs` computes `offset = mean(H(x) - y)` over every location
in the observation space and subtracts it from `H(x)`, and `ObsADTTLAD` does the
same in both the tangent linear and the adjoint. So altimetry is assimilated as
an anomaly about its own domain mean, and a member whose sea level is uniformly
high carries an error no observation reports and no analysis removes. Left in, it
is spread the filter is charged for and cannot use: it inflates every
spread-versus-error diagnostic with a direction the increment cannot reach.

The domain makes that worse rather than better. Measured on `gom_25km`, the
boundary-wide component is 12% of the boundary perturbation's variance and 47% of
the interior sea surface height spread it produces, because FLATHER hands a
boundary-wide head straight to the basin while structured boundary anomalies
mostly radiate back out. About a sevenfold amplification in variance, all of it
into the blind direction.

Only sea surface height. A domain-wide temperature or salinity offset is
observed, by profiles and by SST, and removing it would be removing a real
uncertainty the filter can act on.

Removing a per-member scalar preserves the ensemble mean exactly, since the mean
of the members' offsets is the offset of the mean perturbation, which is zero.
What it does not do is drive the interior basin mean spread to zero: transmission
is not uniform along the boundary, so this removes the cause and leaves a
remainder. The remainder is what the spike measures.
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

#: The field whose boundary-wide component is unobservable, so the one this tool
#: removes it from. MOM6 takes it as `SSH=file:obc.nc(zeta)` on every segment.
HEAD = "zeta"


def boundary_mean(values, segments, field):
    """*field* averaged over every point of every segment, one number per day.

    Unweighted over points rather than by cell width or by area. The quantity
    being stood in for is the mean `ObsADT` removes, which is itself unweighted
    and taken over altimeter locations rather than over the boundary, so no
    weighting available here matches it. The difference between the choices is
    far smaller than the component being removed.

    None when no segment carries the field, which is how a boundary file that
    prescribes no sea surface height passes through untouched.
    """
    present = [values[segment][field] for segment in segments
               if field in segments[segment]]
    if not present:
        return None
    return np.concatenate([v.reshape(len(v), -1) for v in present],
                          axis=1).mean(axis=1)


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
            for value in magnitudes for sign in (1, -1)]
    return lags[:members]


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
        steps = np.diff(times)
        if len(steps) and not np.allclose(steps, 1.0):
            die(f"{source} has a time axis that is not one day per record "
                f"(steps {steps.min():g} to {steps.max():g}), so a lag in days "
                f"is not a shift in records")
        if 2 * span >= len(times):
            die(f"a lag of {span} days either side leaves nothing of a "
                f"{len(times)} record file")

        keep = slice(span, len(times) - span)
        units = f["time"].units
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
                    m[segment][field] = args.amplitude * (m[segment][field] - mean)

        # The sea surface height perturbation loses its boundary-wide part
        # before it is added back. See "The head that nothing can correct".
        removed = []
        for m in members:
            offset = boundary_mean(m, segments, HEAD)
            removed.append(0.0 if offset is None
                           else float(np.sqrt((offset ** 2).mean())))
            if offset is not None:
                for segment, fields in segments.items():
                    if HEAD in fields:
                        m[segment][HEAD] -= offset.reshape(
                            (-1,) + (1,) * (m[segment][HEAD].ndim - 1))

        for segment, fields in segments.items():
            for field in fields:
                for m in members:
                    m[segment][field] += base[segment][field]

        args.out.mkdir(parents=True, exist_ok=True)
        written = [("mem000", 0, base)]
        written += [(f"mem{i + 1:03d}", lag, values)
                    for i, (lag, values) in enumerate(zip(lags, members))]
        print()
        print(f"{'member':8s}{'lag':>6s}   perturbation rms, over the window")
        print(f"{'':8s}{'days':>6s}"
              + "".join(f"{field:>10s}" for field in field_order(segments)))
        for name, lag, values in written:
            write_member(f, args.out / f"{name}.nc", keep, values)
            row = []
            for field in field_order(segments):
                difference = np.concatenate(
                    [(values[segment][field] - base[segment][field]).ravel()
                     for segment in segments if field in segments[segment]])
                row.append(np.sqrt((difference ** 2).mean()))
            print(f"{name:8s}{lag:6d}" + "".join(f"{v:10.4f}" for v in row))
        print("Departure from the unperturbed boundary, over every segment point, "
              "level and day. mem000 is that boundary, so its row is zero.")
        if any(removed):
            print(f"\nBoundary-wide {HEAD} removed from each member, rms over "
                  f"the window: "
                  + ", ".join(f"{v:.4f}" for v in removed))
            print("Unobservable, not small. See the head that nothing can "
                  "correct, above.")

    print(f"\nobc-lagged: wrote {len(written)} files to {args.out}")


def write_member(source, path, keep, values):
    """One member, structurally identical to the file it came from.

    Everything not a segment data field is copied through: `dz_`, `lon`, `lat`,
    the time axis and its attributes. MOM6 reads `<var>_segment_00N` and
    `dz_<var>_segment_00N` and dies if their shapes disagree, so the safe
    edit is the values of one and nothing else.
    """
    perturbed = {variable: values[segment][field]
                 for segment, fields in segments_of(source).items()
                 for field, variable in fields.items()}

    partial = path.with_suffix(".nc.partial")
    with nc.Dataset(partial, "w", format=source.data_model) as out:
        for name, dimension in source.dimensions.items():
            out.createDimension(name, None if dimension.isunlimited()
                                else len(dimension))
        for name, variable in source.variables.items():
            copy = out.createVariable(name, variable.datatype, variable.dimensions)
            copy.setncatts({key: variable.getncattr(key)
                            for key in variable.ncattrs()})
            if name in perturbed:
                copy[:] = perturbed[name]
            elif "time" in variable.dimensions:
                copy[:] = variable[keep]
            else:
                copy[:] = variable[:]
    partial.rename(path)


if __name__ == "__main__":
    main()
