#!/usr/bin/env python3
"""Write the correlation length scale fields the diffusion calibration reads.

    tools/diffusion-scales.py <gridspec> <restart> <config> <outdir>

Called by `tools/soca-diffusion.sh` and not otherwise useful on its own. It
writes one file per entry under `horizontal:` in the config, named
`scales_<entry>.nc`, plus `scales_corr_vt.nc` if `vertical:` is present.

**The science is in `ackbar.diffusion`, not here.** This is the offline half of
two callers: `ackbar.run`'s `b.corr_vt` task builds the same vertical scale field
per cycle, against the background that cycle is about to assimilate into, and
the two have to agree exactly. A normalization is computed for the scale field
it was built from, so two ways of computing that field is a background error
wrong by a factor nothing reports.
"""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ackbar.diffusion import (  # noqa: E402
    horizontal_scales, read_gridspec, read_restart, report, smoothing_scale,
    vertical_scales, write,
)


def main(argv):
    if len(argv) != 5:
        sys.exit(__doc__.strip().splitlines()[2].strip())
    gridspec_path, restart_path, config_path, outdir = argv[1:5]

    with open(config_path) as stream:
        config = yaml.safe_load(stream)

    grid = read_gridspec(gridspec_path)

    # The scale over which the scales themselves are smoothed, in grid cells.
    # Zonally averaged, on the assumption that the Rossby radius varies with
    # latitude far more than with longitude, which is true everywhere the
    # smoothing matters and cheap where it does not.
    smoothing = smoothing_scale(grid)

    for name, spec in (config.get("horizontal") or {}).items():
        scales = horizontal_scales(grid, spec, smoothing)
        write(f"{outdir}/scales_{name}.nc", grid, hz=scales)
        report(f"{name}", scales, grid["mask"], "m")

    vertical = config.get("vertical")
    if vertical:
        h, mld = read_restart(restart_path, grid, smoothing)
        scales = vertical_scales(h, mld, vertical)
        write(f"{outdir}/scales_corr_vt.nc", grid, vt=scales)
        report("vt", scales[0], grid["mask"], "levels")


if __name__ == "__main__":
    main(sys.argv)
