#!/bin/bash
# Check a domain's calibrated diffusion operator by applying it to a dirac.
#
#   tools/soca-dirac.sh <domain> [<lat>,<lon>]... [<mode>] [--keep <dir>]
#   tools/soca-dirac.sh gom_25km
#   tools/soca-dirac.sh gom_8km 26.5,-90.0 28.9,-88.4
#   tools/soca-dirac.sh gom_25km --full --keep ~/dirac
#   tools/soca-dirac.sh gom_25km --ensemble <dir of mem*/MOM.res.nc>
#   tools/soca-dirac.sh gom_25km --hybrid   <dir of mem*/MOM.res.nc>
#
# `--full` applies the whole static background error rather than the correlation
# alone: the standard deviations, the depth taper and the balance operator.
# `--ensemble` applies the localized ensemble covariance instead, and `--hybrid`
# applies both weighted together and each of them separately. See the section
# below and the header of tools/dirac.py.
#
# `--keep` copies the increment, the document that produced it and the placement
# record into a directory instead of dropping them with the working directory.
# The increment is a SOCA increment on the model grid, so it opens in anything
# that opens a MOM6 restart: `Temp` carries the temperature diracs at their own
# levels and `ave_ssh` the height ones. The report below is the same field read
# through a ruler, and looking at the blob is the way to see the thing the ruler
# cannot describe, which is its shape near a coast.
#
# With no locations it picks two: the deepest ocean cell, and the ocean cell with
# the shortest horizontal scale. Those are the two regimes the calibration has,
# open ocean where the scale is the Rossby radius and shelf where it is the grid
# floor, and a calibration that is wrong is usually wrong in only one of them.
#
# ---------------------------------------------------------------------------
# What it proves, and why the peak value is the whole test
#
# A correlation operator applied to a dirac returns, at the dirac's own point,
# exactly 1. That is what normalizing it means. The horizontal normalization is
# a Monte Carlo estimate, so what comes back is 1 plus the estimate's error, and
# that error is the only direct measurement of whether `normalization
# iterations` in config/static/diffusion.yaml was set high enough. A peak of 0.97 is
# not a rounding difference: it says every increment through this B is 3 percent
# small, at that point, in a way nothing downstream reports.
#
# The second number it reports is the distance at which the response falls to
# e^-0.5 of its peak, which for a Gaussian kernel is the length scale itself. Put
# beside the scale the calibration was asked for, it says whether the operator
# built the correlation that was ordered. It is approximate for the vertical,
# where the implicit scheme's kernel is Matern-like rather than Gaussian.
#
# ---------------------------------------------------------------------------
# It reads the experiment's own B
#
# The saber blocks below are lifted out of the `da/*` layers rather than written
# here, and every mode but the default assembles them with the same
# `ackbar.soca.background_error` an analysis job calls. A dirac test against a
# second description of the background error would pass while the analysis used
# a different operator, which is the failure it exists to catch.
#
# By default it drops everything outside the central block: the standard
# deviations, the depth taper and the balance operator. Those turn a correlation
# into a covariance, and including them would leave the peak value meaning
# nothing, because a covariance does not return 1 at its own dirac.
#
# `--full` keeps them, and is the other test. There is then no peak to check and
# nothing passes or fails; what comes back is the increment one observation
# would produce, in every variable, including the ones the balance operator
# reached rather than the dirac. It also writes the standard deviation fields
# the parametric block built, which are otherwise never visible.
#
# `--ensemble <dir>` and `--hybrid <dir>` are the same test again over the other
# two covariances an experiment can ask for, reading the member restarts in
# `<dir>/mem*/MOM.res.nc`. The ensemble one has no balance operator in it, so a
# response in a variable the dirac was not placed in is the sample covariance,
# and the localization is what decides how far it reaches. `--hybrid` writes
# three increments rather than one: the hybrid and each component alone.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

DOMAIN=${1:?usage: soca-dirac.sh <domain> [<lat>,<lon>]... [<mode>] [--keep <dir>]}
shift
POINTS=()
KEEP=
# The mode flag, whole, passed to both halves of dirac.py so that the document
# and the report cannot disagree about which covariance was applied. Empty means
# the correlation alone.
MODE=()
while [[ $# -gt 0 ]]; do
    case $1 in
        --full) MODE=(--full); shift ;;
        --ensemble|--hybrid)
            MODE=("$1" "$(cd "${2:?$1 needs a directory of members}" && pwd)")
            shift 2 ;;
        --keep) KEEP=${2:?--keep needs a directory}; shift 2 ;;
        -*) echo "soca-dirac: unknown option $1" >&2; exit 1 ;;
        *) POINTS+=("$1"); shift ;;
    esac
done

source "$ACKBAR_ROOT/tools/domain-paths.sh"
domain_paths "$DOMAIN"

GRIDSPEC=$STATIC/soca_gridspec.nc
TOOLBOX=$ACKBAR_ROOT/pkg/jedi/build/bin/soca_error_covariance_toolbox.x
# The layer *tree*, not one file: which layers a mode merges is dirac.py's, on
# the same rule that says the covariance is assembled the way ackbar assembles
# it. See `STACK` there.
LAYERS=$ACKBAR_ROOT/config/layers
NAMELIST=$ACKBAR_ROOT/config/model/mom6sis2/mom_input.nml
METADATA=$ACKBAR_ROOT/config/model/mom6sis2/fields_metadata.yaml
RESTART=${ACKBAR_DIRAC_RESTART:-}

if [[ -z $RESTART ]]; then
    # `ic/<domain>/<product>/<date>/MOM.res.nc`, and no deeper. An initial
    # condition that also carries a perturbed ensemble keeps it in a
    # subdirectory beside that file, and those are the same state six more
    # times rather than six more states to choose between. What this refuses to
    # guess at is which product or which date, never which member.
    mapfile -t candidates < <(
        find "$ACKBAR_STATIC_ROOT/ic/$DOMAIN" -maxdepth 3 -name MOM.res.nc \
            2>/dev/null | sort)
    [[ ${#candidates[@]} -eq 1 ]] || {
        echo "soca-dirac: set ACKBAR_DIRAC_RESTART; found ${#candidates[@]} candidates" >&2
        exit 1
    }
    RESTART=${candidates[0]}
fi

REQUIRED=("$TOOLBOX" "$LAYERS" "$NAMELIST" "$METADATA" "$GRIDSPEC" "$RESTART"
          "$STATIC/diffusion/corr_hz.nc" "$STATIC/diffusion/corr_hz_ssh.nc"
          "$STATIC/diffusion/corr_vt.nc")
# The correlation alone reads none of these. Everything else reads the standard
# deviations, so it needs the file they are floored by (tools/sst-bgerr.py), and
# any mode with an ensemble in it reads the localization the same offline stage
# calibrated.
[[ ${#MODE[@]} -gt 0 ]] && REQUIRED+=("$STATIC/sst_bgerr.nc")
case ${MODE[0]:-} in
    --ensemble|--hybrid) REQUIRED+=("$STATIC/diffusion/loc_hz.nc" "${MODE[1]}") ;;
esac

for path in "${REQUIRED[@]}"; do
    [[ -e $path ]] || {
        echo "soca-dirac: $path does not exist" >&2
        echo "soca-dirac: run tools/soca-diffusion.sh $DOMAIN first" >&2
        exit 1
    }
done

WORK=$(mktemp -d "${TMPDIR:-/tmp}/soca-dirac.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

# The working directory every SOCA application needs. See tools/soca-gridspec.sh
# for why each of these is here and why the namelist is not called input.nml.
ln -s "$DATA" INPUT
cp "$BASE/MOM_input" .
cp "$OVERRIDE/MOM_override" MOM_override
cp "$OVERRIDE/MOM_override.soca" MOM_override.soca
cp "$NAMELIST" mom_input.nml
printf 'soca_dirac\n1 1 1 0 0 0\n' > diag_table
ln -s "$GRIDSPEC" soca_gridspec.nc
mkdir -p out

python3 "$ACKBAR_ROOT/tools/dirac.py" plan \
    "$LAYERS" "$STATIC" "$LEVELS" "$METADATA" "$GRIDSPEC" "$RESTART" \
    dirac.yaml points.json ${POINTS[@]+"${POINTS[@]}"} ${MODE[@]+"${MODE[@]}"}

# How many ranks is a property of the machine, so the site file owns it and
# nothing here may name a number. See site/rancor.sh.
#
# Overridable per invocation because this runs outside Slurm, so nothing
# schedules it against whatever else is on the box. The tier 3 suite is the
# case that bites: its experiment tests go through Slurm while this goes
# straight to mpiexec, and the two together can oversubscribe the node.
NTASKS=${NTASKS:-${ACKBAR_MPI_TASKS:?the site did not set ACKBAR_MPI_TASKS}}
echo "soca-dirac: applying the correlation to the diracs on $NTASKS ranks"
mpiexec -n "$NTASKS" "$TOOLBOX" dirac.yaml > toolbox.log 2>&1 || {
    tail -40 toolbox.log >&2
    echo "soca-dirac: the toolbox failed; the log above is its last 40 lines" >&2
    exit 1
}

# How many sweeps the operator actually costs, which is the one number that says
# whether these scales are affordable and it is nowhere in the configuration.
# oops derives the horizontal count from stability: it needs
# niter >= 2 (scale/spacing)^2 times a grid aspect ratio, and it takes the
# **maximum over every cell in the domain**, so one cell where the scale is long
# against its own grid spacing sets the price for all of them. The vertical
# count is whatever `config/static/diffusion.yaml` states, because implicit does not
# have a stability limit to satisfy.
#
# One line per group, in the order the groups are declared, which is why the
# horizontal count appears more than once: each scale file gets its own
# operator and the longer scales cost more.
#
# `|| true`, because this is a print and not a check. Under `pipefail` a grep
# that matches nothing exits 1, `set -e` takes the script down before the report
# runs, and the EXIT trap deletes the increment: a successful toolbox run thrown
# away because a log line was reworded upstream.
grep "Diffusion: \(horizontal\|vertical\) iterations" toolbox.log |
    sed 's/^.*Diffusion: /soca-dirac: /' || true

# `|| status=$?` rather than a bare call: the report exits non-zero when a check
# fails, and `set -e` would take the script down with it before `--keep` ran.
status=0
python3 "$ACKBAR_ROOT/tools/dirac.py" report "$GRIDSPEC" "$STATIC" points.json out \
    ${MODE[@]+"${MODE[@]}"} || status=$?

# After the report, and unconditionally: a failing check is exactly when the
# increment is worth opening.
if [[ -n $KEEP ]]; then
    mkdir -p "$KEEP"
    cp out/*.nc dirac.yaml points.json "$KEEP/"
    # Only --full writes this. See `save diagnostics` in tools/dirac.py.
    [[ -e stddev.nc ]] && cp stddev.nc "$KEEP/"
    echo "soca-dirac: kept the increment and its document in $KEEP"
fi
exit $status
