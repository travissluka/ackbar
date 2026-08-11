#!/usr/bin/env python3
"""Turn fetched GEFSv12 reforecast GRIB into one `atm.nc` per member.

    tools/spike/spike-gefs-atm.py --gefs /data/ackbar/spike/gefs --hours 120

Writes `<gefs>/<member>/atm.nc` carrying the seven fields MOM6-SIS2's data
table wants, on a box around the Gulf, named the way soca-science named them
(`T2`, `Q2`, `U10`, `V10`, `DSWRF`, `DLWRF`, `PRATE`) so that its data table
can be reused verbatim.

The file's shape is dictated by FMS, not chosen. `data_override` reaches a
field through `time_interp_external`, which requires the time axis to be the
file's *unlimited* dimension, and fails inside `netcdf_io::get_variable_id`
with nothing said about time when it is not. So there is one axis, named
`TIME`, unlimited, and the flux fields are interpolated onto it rather than
carrying an axis of their own.

Two conversions are not passthroughs and are the only places this can be
silently wrong:

  - `apcp` is accumulated over each three hour interval, and MOM6 wants a rate,
    so it is divided by the interval. A file whose accumulation reset period
    differs from its output interval would need the difference taken first, and
    the check below that every message spans exactly one output interval is
    what stands between that case and a precipitation field too large by the
    number of intervals since the last reset.
  - `dswrf` and `dlwrf` are interval means, so they are stamped at the interval
    midpoint rather than at its end. Stamping a mean at the end of its window
    shifts the diurnal cycle of shortwave by half an interval, which over five
    days is a systematic warm or cold bias in the mixed layer and not noise.

Interpolating an interval mean onto instantaneous times is a real
approximation, and it is the shortwave that pays: a six hour mean sampled every
three hours cannot carry a sharper diurnal cycle than six hours. That is the
price of the single axis FMS requires, and it is charged to all five members
equally, so it moves the ensemble mean and not the spread this spike measures.
"""

import argparse
from pathlib import Path

import eccodes
import netCDF4
import numpy

#: GRIB file stem -> (output name, units, how the time stamp is chosen).
#:
#: `mid` marks a field whose value is a mean or an accumulation over the
#: interval ending at its step.
FIELDS = {
    "tmp_2m":    ("T2",    "K",             "instant"),
    "spfh_2m":   ("Q2",    "kg kg-1",       "instant"),
    "ugrd_hgt":  ("U10",   "m s-1",         "instant"),
    "vgrd_hgt":  ("V10",   "m s-1",         "instant"),
    "dswrf_sfc": ("DSWRF", "W m-2",         "mid"),
    "dlwrf_sfc": ("DLWRF", "W m-2",         "mid"),
    "apcp_sfc":  ("PRATE", "kg m-2 s-1",    "mid"),
}

MEMBERS = ("c00", "p01", "p02", "p03", "p04")

#: A box around gom_25km (lon -98 to -76.5, lat 18.1 to 31.9) with enough
#: margin that FMS's bilinear interpolation never reaches past the edge.
BOX = {"west": 258.0, "east": 288.0, "south": 14.0, "north": 36.0}


def read(path, hours):
    """Every message in *path* out to *hours*, as (steps, lon, lat, cube)."""
    steps, planes, spans = [], [], []
    lons = lats = None
    with open(path, "rb") as stream:
        while True:
            handle = eccodes.codes_grib_new_from_file(stream)
            if handle is None:
                break
            try:
                end = eccodes.codes_get(handle, "endStep")
                if end > hours:
                    continue
                start = eccodes.codes_get(handle, "startStep")
                ni = eccodes.codes_get(handle, "Ni")
                nj = eccodes.codes_get(handle, "Nj")
                if lons is None:
                    first_lon = eccodes.codes_get(handle, "longitudeOfFirstGridPointInDegrees")
                    first_lat = eccodes.codes_get(handle, "latitudeOfFirstGridPointInDegrees")
                    step_lon = eccodes.codes_get(handle, "iDirectionIncrementInDegrees")
                    step_lat = eccodes.codes_get(handle, "jDirectionIncrementInDegrees")
                    lons = (first_lon + step_lon * numpy.arange(ni)) % 360.0
                    # GRIB scans north to south, so the latitude increment is a
                    # decrement. Reading it as positive silently flips the
                    # field about the equator, which in this box is a plausible
                    # looking wind that blows the wrong way.
                    lats = first_lat - step_lat * numpy.arange(nj)
                values = eccodes.codes_get_values(handle).reshape(nj, ni)
                steps.append(end)
                spans.append(end - start)
                planes.append(values)
            finally:
                eccodes.codes_release(handle)
    if not planes:
        raise SystemExit(f"spike-gefs-atm: no messages within {hours} h in {path}")
    order = numpy.argsort(steps)
    return (numpy.array(steps)[order], numpy.array(spans)[order],
            lons, lats, numpy.stack(planes)[order])


def subset(lons, lats, cube):
    """Clip to BOX and return coordinates increasing, which is what FMS wants."""
    keep_x = numpy.where((lons >= BOX["west"]) & (lons <= BOX["east"]))[0]
    keep_y = numpy.where((lats >= BOX["south"]) & (lats <= BOX["north"]))[0]
    out = cube[:, keep_y, :][:, :, keep_x]
    x, y = lons[keep_x], lats[keep_y]
    if y[0] > y[-1]:
        y, out = y[::-1], out[:, ::-1, :]
    return x, y, out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gefs", type=Path, required=True)
    ap.add_argument("--hours", type=int, default=120,
                    help="leads to keep, so 120 covers a five day forecast")
    ap.add_argument("--init", default="2015071200",
                    help="YYYYMMDDHH the reforecast was initialised at; every "
                         "lead in the GRIB is an offset from it")
    args = ap.parse_args()

    origin = (f"{args.init[0:4]}-{args.init[4:6]}-{args.init[6:8]} "
              f"{args.init[8:10]}:00:00")

    for member in MEMBERS:
        source = args.gefs / member
        target = source / "atm.nc"
        if not source.is_dir():
            raise SystemExit(f"spike-gefs-atm: {source} does not exist")

        with netCDF4.Dataset(target, "w", format="NETCDF3_64BIT_OFFSET") as out:
            first = True
            axis = None
            for stem, (name, units, stamp) in FIELDS.items():
                steps, spans, lons, lats, cube = read(source / f"{stem}.grib2", args.hours)
                x, y, cube = subset(lons, lats, cube)

                if stamp == "mid":
                    # NCEP resets accumulations and averages every six hours and
                    # reports at three, so a field arrives as an alternating
                    # sequence of windows: 0-3, 0-6, 6-9, 6-12, and so on. The
                    # three hour records overlap the six hour ones, and simply
                    # taking every message would count the first half of each
                    # reset period twice.
                    #
                    # Keeping only the full-length windows leaves a set that
                    # tiles the forecast exactly once, at six hourly resolution.
                    # Recovering the second three hour half by differencing is
                    # possible and buys nothing here: the ocean is forced
                    # through bulk formulae over a day, and the diurnal cycle
                    # this would sharpen is already carried by the shortwave
                    # window's own midpoint.
                    full = spans == spans.max()
                    steps, spans, cube = steps[full], spans[full], cube[full]
                    edges = steps - spans
                    if not numpy.array_equal(edges[1:], steps[:-1]):
                        raise SystemExit(
                            f"spike-gefs-atm: {stem}'s {spans.max()} h windows do "
                            f"not tile the forecast: starts {edges.tolist()} "
                            f"against ends {steps.tolist()}")
                    when = steps - spans / 2.0
                else:
                    when = steps.astype(float)

                if name == "PRATE":
                    cube = cube / (spans[:, None, None] * 3600.0)

                if first:
                    # The first field read is instantaneous, so its stamps
                    # become the file's one axis and every later field is put
                    # onto them.
                    #
                    # Extended back to the initialisation hour by holding the
                    # first record. The reforecast's first output is at +3 h and
                    # a forecast started at 00Z asks for 00Z immediately, which
                    # `time_interp_external` refuses rather than extrapolating:
                    # "time ... is before range of list". Holding three hours of
                    # a six hourly atmosphere is a smaller lie than the run not
                    # starting.
                    when = numpy.concatenate([[0.0], when])
                    cube = numpy.concatenate([cube[:1], cube])
                    axis = when
                    out.createDimension("LON", len(x))
                    out.createDimension("LAT", len(y))
                    out.createDimension("TIME", None)
                    v = out.createVariable("LON", "f8", ("LON",))
                    v.units, v.long_name, v.axis = "degrees_east", "longitude", "X"
                    v[:] = x
                    v = out.createVariable("LAT", "f8", ("LAT",))
                    v.units, v.long_name, v.axis = "degrees_north", "latitude", "Y"
                    v[:] = y
                    t = out.createVariable("TIME", "f8", ("TIME",))
                    t.units = f"hours since {origin}"
                    t.calendar = "JULIAN"
                    t.axis = "T"
                    t[:] = axis
                    first = False
                elif not numpy.array_equal(when, axis):
                    # numpy.interp clamps outside the range rather than
                    # extrapolating, which is what should happen at the ends of
                    # a forecast: the first and last flux windows are held, not
                    # projected past data that does not exist.
                    cube = numpy.stack([
                        numpy.interp(axis, when, cube[:, j, i])
                        for j in range(cube.shape[1]) for i in range(cube.shape[2])
                    ], axis=1).reshape(len(axis), cube.shape[1], cube.shape[2])

                field = out.createVariable(name, "f4", ("TIME", "LAT", "LON"),
                                           fill_value=-1.0e34)
                field.missing_value = numpy.float32(-1.0e34)
                field.units = units
                field[:] = cube

            out.source = "GEFSv12 reforecast, NOAA-PSL, via tools/spike/spike-gefs-fetch.sh"
        print(f"{target}  {target.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
