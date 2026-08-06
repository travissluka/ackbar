#!/bin/bash
# Calibrate a domain's diffusion operator: the background error's correlation.
#
#   tools/soca-diffusion.sh <domain> [<restart>] [--iterations N]
#   tools/soca-diffusion.sh gom_25km
#   tools/soca-diffusion.sh gom_25km --iterations 200    # smoke test only
#
# Writes `hz.nc`, `hz_ssh.nc` and `vt.nc` into
# $ACKBAR_STATIC_ROOT/static/<domain>/diffusion, which is what the `filepath`
# entries under `solver.background error` in `config/layers/da/variational.yaml`
# name. Until this has run, `ackbar validate` reports those three as missing
# inputs, which is correct and is the intended way to find out.
#
# Offline and keyed on domain, exactly like `tools/soca-gridspec.sh` and for the
# same reason: it is expensive, it is a constant, and every experiment on the
# domain has to be reading the same one or two analyses are not comparable.
#
# ---------------------------------------------------------------------------
# What it actually does
#
#   1. `tools/diffusion-scales.py` turns the gridspec's Rossby radius and the
#      restart's mixed layer into length scale *fields*, one per group, written
#      as files SOCA can read as though they were model states.
#   2. `soca_error_covariance_toolbox.x` builds a diffusion operator from each
#      of those, and estimates by randomization the normalization that makes the
#      operator a correlation, with ones on the diagonal.
#   3. Step 2's output is what an analysis reads.
#
# Step 2 is the expensive one and the reason this is not a per-cycle task. Step 1
# is a single pass over the grid.
#
# ---------------------------------------------------------------------------
# Two things to know before trusting the output
#
#   * The vertical scales come from `MLD` in the restart you give it, so they
#     describe *that* state's mixed layer. A cold start's mixed layer is thinner
#     than a spun up one, so a B calibrated on an initial condition is tighter in
#     the vertical than it should be. Recalibrate against a spun up background
#     before believing an analysis. v2 recalibrated the vertical every cycle;
#     ACKBAR does not yet, and this is the reason that is on the list.
#
#   * The horizontal normalization is a Monte Carlo estimate, so it depends on
#     the random draws and therefore on the MPI decomposition. Two runs at
#     different rank counts give slightly different files. That is a difference
#     of a fraction of a percent in increment amplitude, not a difference in
#     structure, but it does mean this is not bit reproducible across a change
#     in `-n` below.
#
# Rerun it after a change to `config/static/diffusion.yaml`, after a change to the
# domain's grid, and after a bundle bump that touches saber's diffusion block.
# Nothing detects a stale calibration.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

DOMAIN=${1:?usage: soca-diffusion.sh <domain> [<restart>] [--iterations N]}
shift
RESTART=
# `--iterations` overrides `normalization iterations` in config/static/diffusion.yaml,
# and exists for one purpose: proving the stage runs, in seconds rather than in
# tens of minutes. A calibration built at a low count is not a calibration. It
# is normalized by a Monte Carlo estimate whose error falls as 1/sqrt(N), so at
# 200 iterations the operator's diagonal is off by percent-scale amounts that
# vary from cell to cell, and an analysis using it is wrong in a way that looks
# like noisy tuning rather than like a mistake. The config file's value is the
# science value. Nothing downstream can tell the two apart, which is why the
# generated documents are copied next to the output: they are the only record of
# what a given `hz.nc` was normalized with.
ITERATIONS=
while [[ $# -gt 0 ]]; do
    case $1 in
        --iterations) ITERATIONS=${2:?--iterations needs a count}; shift 2 ;;
        -*) echo "soca-diffusion: unknown option $1" >&2; exit 1 ;;
        *) RESTART=$1; shift ;;
    esac
done

source "$ACKBAR_ROOT/tools/domain-paths.sh"
domain_paths "$DOMAIN"

# $STATIC is the domain layer's `domain_static`, which is also what the
# experiment's `filepath` entries resolve to. Read rather than rebuilt here, so
# that this writes where the analysis looks by construction.
GRIDSPEC=$STATIC/soca_gridspec.nc
OUT=$STATIC/diffusion

TOOLBOX=$ACKBAR_ROOT/pkg/jedi/build/bin/soca_error_covariance_toolbox.x
SCALES=$ACKBAR_ROOT/tools/diffusion-scales.py
CONFIG=$ACKBAR_ROOT/config/static/diffusion.yaml
NAMELIST=$ACKBAR_ROOT/config/model/mom6sis2/mom_input.nml
METADATA=$ACKBAR_ROOT/config/model/mom6sis2/fields_metadata.yaml

# The restart supplies the mixed layer the vertical scales are built from, and
# the background the calibration is nominally centred on. Discovered rather than
# assumed, and only when there is exactly one candidate: picking one of several
# initial conditions silently is how a domain ends up with a B nobody can
# account for.
if [[ -z $RESTART ]]; then
    # `ensemble*` is pruned for the same reason `tools/ensemble-ic.sh` prunes
    # it: those directories are members drawn *from* a calibration, so offering
    # one as the state to calibrate against is a loop, and there are as many of
    # them as the ensemble is large.
    mapfile -t candidates < <(
        find "$ACKBAR_STATIC_ROOT/ic/$DOMAIN" \
            -type d -name 'ensemble*' -prune -o \
            -name MOM.res.nc -print 2>/dev/null | sort)
    if [[ ${#candidates[@]} -eq 1 ]]; then
        RESTART=${candidates[0]}
        echo "soca-diffusion: using the only staged initial condition,"
        echo "soca-diffusion:   $RESTART"
    elif [[ ${#candidates[@]} -eq 0 ]]; then
        echo "soca-diffusion: $DOMAIN has no staged initial condition." >&2
        echo "soca-diffusion: run tools/coldstart-ic.sh, or name a restart." >&2
        exit 1
    else
        echo "soca-diffusion: name the restart to calibrate against." >&2
        printf 'soca-diffusion:   %s\n' "${candidates[@]}" >&2
        exit 1
    fi
fi

for path in "$TOOLBOX" "$SCALES" "$CONFIG" "$NAMELIST" "$METADATA" \
            "$GRIDSPEC" "$RESTART" "$BASE/MOM_input" \
            "$OVERRIDE/MOM_override" "$OVERRIDE/MOM_override.soca" "$DATA"; do
    [[ -e $path ]] || { echo "soca-diffusion: $path does not exist" >&2; exit 1; }
done

WORK=$(mktemp -d "${TMPDIR:-/tmp}/soca-diffusion.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

# The same working directory a SOCA application always needs: the model's text
# from the repository, its data from the static root, and a diag_table that
# nothing writes to but FMS refuses to start without. See tools/soca-gridspec.sh,
# which explains each of these and why the namelist may not be called input.nml.
ln -s "$DATA" INPUT
cp "$BASE/MOM_input" .
cp "$OVERRIDE/MOM_override" MOM_override
cp "$OVERRIDE/MOM_override.soca" MOM_override.soca
cp "$NAMELIST" mom_input.nml
printf 'soca_diffusion\n1 1 1 0 0 0\n' > diag_table
ln -s "$GRIDSPEC" soca_gridspec.nc
mkdir -p out

echo "soca-diffusion: building length scales for $DOMAIN in $WORK"
python3 "$SCALES" "$GRIDSPEC" "$RESTART" "$CONFIG" .

# The calibration documents. Generated from `config/static/diffusion.yaml` rather than
# kept beside it as templates, so that the group names, the file names and the
# vertical scheme have exactly one source and cannot disagree.
#
# Two documents and two runs, horizontal and vertical, which looks like a
# needless split and is not. saber reads a group's scale field by building a
# whole increment over the calibration's *active* variables and then picking one
# field out of it, so every scale file has to contain every active variable. One
# run over both would mean writing a full three dimensional field into the
# horizontal scale files for saber to read and discard, and it would run the
# horizontal randomization over every level instead of over one. v2 split them
# for the same reason.
python3 - "$CONFIG" "$METADATA" "$RESTART" "$ITERATIONS" \
         calibrate_hz.yaml calibrate_vt.yaml <<'PY'
import os, sys, yaml

config_path, metadata, restart, iterations, hz_out, vt_out = sys.argv[1:7]
config = yaml.safe_load(open(config_path))
if iterations:
    config["normalization iterations"] = int(iterations)

# A label, not a lookup. SOCA takes the validity time of a state read with an
# explicit filename from the configuration and never compares it against the
# file, so this date says only "these fields are all the same time as each
# other". Using the restart's real date would imply a check that does not happen.
DATE = "2000-01-01T00:00:00Z"

# The active variable of each run. Horizontal calibration runs on a surface
# field because its scales are two dimensional, which is what keeps the
# randomization to one level rather than to the whole column. Neither name has
# anything to do with what the field means: the file holds a length in metres or
# in levels, and this is only how saber is told to go and find it.
HZ_VARIABLE = "sea_surface_height_above_geoid"
VT_VARIABLE = "sea_water_potential_temperature"


def model_file(name):
    return {"date": DATE, "basename": "./", "ocn_filename": f"scales_{name}.nc"}


def document(variable, groups):
    return {
        "geometry": {
            "geom_grid_file": "soca_gridspec.nc",
            "mom6_input_nml": "mom_input.nml",
            "fields metadata": metadata,
        },
        "background": {
            "read_from_file": 1,
            "basename": os.path.dirname(restart) + "/",
            "ocn_filename": os.path.basename(restart),
            "date": DATE,
            "state variables": [variable],
        },
        "background error": {
            "covariance model": "SABER",
            "saber central block": {
                "saber block name": "diffusion",
                "calibration": {
                    "normalization": {
                        "iterations": config["normalization iterations"]},
                    "groups": groups,
                },
            },
        },
    }


# `as gaussian` is what stops saber applying its Gaspari-Cohn to Daley
# conversion. diffusion-scales.py already emits a Gaussian sigma, which is the
# Daley length of the kernel, so the conversion would shrink every scale by 3.67
# for no reason.
horizontal = [
    {"horizontal": {"as gaussian": True,
                    "model file": model_file(name),
                    "model variable": HZ_VARIABLE},
     "write": {"filepath": f"out/{name}"}}
    for name in (config.get("horizontal") or {})
]

vertical = config.get("vertical")
vertical = [] if not vertical else [
    {"vertical": {"as gaussian": True,
                  "method": vertical["method"],
                  "iterations": vertical["iterations"],
                  "model file": model_file("vt"),
                  "model variable": VT_VARIABLE},
     "write": {"filepath": "out/vt"}}
]

for path, variable, groups in ((hz_out, HZ_VARIABLE, horizontal),
                               (vt_out, VT_VARIABLE, vertical)):
    with open(path, "w") as stream:
        yaml.safe_dump(document(variable, groups), stream,
                       sort_keys=False, default_flow_style=False)
PY

# Eight ranks because that is what this machine has cores for and the
# randomization is the whole cost. See the note above about this not being bit
# reproducible across a change to this number.
echo "soca-diffusion: calibrating the horizontal"
mpiexec -n 8 "$TOOLBOX" calibrate_hz.yaml
echo "soca-diffusion: calibrating the vertical"
mpiexec -n 8 "$TOOLBOX" calibrate_vt.yaml

# What the toolbox writes is what the da layers read, so check for the files
# rather than for an exit status. `filepath` in saber is a stem: the file is the
# stem plus `.nc`.
#
# The list comes from the config rather than being written out here, so that
# adding a group is one edit rather than three. `loc_hz` arrived that way.
mapfile -t NAMES < <(python3 -c '
import sys, yaml
config = yaml.safe_load(open(sys.argv[1]))
print("\n".join(config.get("horizontal") or {}))
if config.get("vertical"):
    print("vt")
' "$CONFIG")

for name in "${NAMES[@]}"; do
    [[ -s out/$name.nc ]] || {
        echo "soca-diffusion: $TOOLBOX exited 0 and wrote no out/$name.nc" >&2
        exit 1
    }
done

mkdir -p "$OUT"
for name in "${NAMES[@]}"; do mv "out/$name.nc" "$OUT/"; done
cp calibrate_hz.yaml calibrate_vt.yaml "$OUT/"
echo "soca-diffusion: wrote ${NAMES[*]/#/$OUT/} (with .nc)"
echo "soca-diffusion: verify it with  tools/soca-dirac.sh $DOMAIN"
