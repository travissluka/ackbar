#!/bin/bash
# Build a domain's ensemble initial condition: one restart set per member.
#
#   tools/ensemble-ic.sh <domain> <members> [<initial condition>]
#   tools/ensemble-ic.sh gom_25km 10
#   tools/ensemble-ic.sh gom_25km 20 $ACKBAR_STATIC_ROOT/ic/gom_25km/hycom-smoke/20150105T01
#
# Writes `<initial condition>/ensemble<members>/mem001..N/`, beside the state it
# perturbed, because that is what it is: the same initial condition, spread. An
# experiment names the directory with `ensemble.initial_condition`.
#
# Offline and keyed on domain, like the gridspec and the background error, and
# for the same reason: two experiments whose ensembles start from different
# states are not comparable and nothing downstream would say so.
#
# ---------------------------------------------------------------------------
# What it is, and what it is not
#
# `soca_enspert.x` draws each member from the experiment's own static background
# error: the same B the variational analysis uses, correlation and standard
# deviations both. So the spread it produces is, by construction, the spread
# that B claims the background has.
#
# That is the right starting point and the wrong ensemble. The perturbations are
# static: they carry the correlation length scales of B and none of the flow
# structure of the ocean at this instant, so the ensemble has spread but no
# dynamical balance. The first cycle's forecast is what fixes that, and the
# first few cycles of any experiment started this way should be read as spin-up.
#
# A better ensemble is a set of states from a long run, sampled far enough apart
# to be independent. That needs a long run, which needs forcing and boundary
# data this domain does not yet have, and it is what an OSSE nature run
# eventually provides. See "After that: regional" in docs/build-order.md.
#
# ---------------------------------------------------------------------------
# It reads the experiment's own B
#
# The background error below is lifted whole out of
# `config/layers/da/variational.yaml`, outer blocks included. That differs from
# the dirac test, which takes the central block alone, and the difference is the
# point: a dirac measures a correlation and the standard deviations would spoil
# the reading, while a perturbation *is* a draw from the covariance and the
# standard deviations are exactly what set its size.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

DOMAIN=${1:?usage: ensemble-ic.sh <domain> <members> [<initial condition>]}
MEMBERS=${2:?usage: ensemble-ic.sh <domain> <members> [<initial condition>]}
IC=${3:-}

source "$ACKBAR_ROOT/tools/domain-paths.sh"
domain_paths "$DOMAIN"

GRIDSPEC=$STATIC/soca_gridspec.nc
ENSPERT=$ACKBAR_ROOT/pkg/jedi/build/bin/soca_enspert.x
LAYER=$ACKBAR_ROOT/config/layers/da/variational.yaml
NAMELIST=$ACKBAR_ROOT/config/model/mom6sis2/mom_input.nml
METADATA=$ACKBAR_ROOT/config/model/mom6sis2/fields_metadata.yaml

# Exactly one initial condition, or say which ones there are. Same rule as
# tools/soca-dirac.sh: guessing between two is how an ensemble ends up built
# from a state nobody meant.
if [[ -z $IC ]]; then
    # Pruning `ensemble*` matters: this stage writes its own output beside the
    # state it perturbed, so without it a second run offers the members of the
    # first as initial conditions to perturb again.
    mapfile -t candidates < <(
        find "$ACKBAR_STATIC_ROOT/ic/$DOMAIN" \
            -type d -name 'ensemble*' -prune -o \
            -name coupler.res -printf '%h\n' 2>/dev/null | sort)
    if [[ ${#candidates[@]} -eq 1 ]]; then
        IC=${candidates[0]}
    elif [[ ${#candidates[@]} -eq 0 ]]; then
        echo "ensemble-ic: no initial condition under $ACKBAR_STATIC_ROOT/ic/$DOMAIN" >&2
        echo "ensemble-ic: run tools/coldstart-ic.sh $DOMAIN first" >&2
        exit 1
    else
        echo "ensemble-ic: name one; $ACKBAR_STATIC_ROOT/ic/$DOMAIN holds" >&2
        printf 'ensemble-ic:   %s\n' "${candidates[@]}" >&2
        exit 1
    fi
fi

TARGET=$IC/ensemble$MEMBERS

for path in "$ENSPERT" "$LAYER" "$NAMELIST" "$METADATA" "$GRIDSPEC" \
            "$IC/MOM.res.nc" "$IC/coupler.res" \
            "$STATIC/diffusion/corr_hz.nc" "$STATIC/diffusion/corr_vt.nc"; do
    [[ -e $path ]] || {
        echo "ensemble-ic: $path does not exist" >&2
        echo "ensemble-ic: the domain needs its static stage and its background" >&2
        echo "ensemble-ic: error; see tools/soca-gridspec.sh and tools/soca-diffusion.sh" >&2
        exit 1
    }
done

WORK=$(mktemp -d "${TMPDIR:-/tmp}/ensemble-ic.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

# The working directory every SOCA application needs. See tools/soca-gridspec.sh
# for why each of these is here and why the namelist is not called input.nml.
ln -s "$DATA" INPUT
cp "$BASE/MOM_input" .
cp "$OVERRIDE/MOM_override" MOM_override
cp "$OVERRIDE/MOM_override.soca" MOM_override.soca
cp "$NAMELIST" mom_input.nml
printf 'ensemble_ic\n1 1 1 0 0 0\n' > diag_table
ln -s "$GRIDSPEC" soca_gridspec.nc
mkdir -p out

cat > place.yaml <<EOF
domain: $DOMAIN
static: $STATIC
metadata: $METADATA
layer: $LAYER
EOF

python3 "$ACKBAR_ROOT/tools/ensemble-ic.py" plan \
    "$LAYER" "$STATIC" "$LEVELS" "$METADATA" "$IC/MOM.res.nc" \
    "$MEMBERS" enspert.yaml

# How many ranks is a property of the machine, so the site file owns it and
# nothing here may name a number. Overridable per invocation because this runs
# outside Slurm and nothing schedules it against what else is on the box.
NTASKS=${NTASKS:-${ACKBAR_MPI_TASKS:?the site did not set ACKBAR_MPI_TASKS}}
echo "ensemble-ic: drawing $MEMBERS perturbation(s) from the static background error"
mpiexec -n "$NTASKS" "$ENSPERT" enspert.yaml > enspert.log 2>&1 || {
    tail -40 enspert.log >&2
    echo "ensemble-ic: soca_enspert.x failed; the log above is its last 40 lines" >&2
    exit 1
}

python3 "$ACKBAR_ROOT/tools/ensemble-ic.py" place \
    place.yaml "$IC" out "$WORK/ensemble"

# Moved into place only once every member is whole, so an interrupted run leaves
# no half-built ensemble for an experiment to start from.
written=$(find "$WORK/ensemble" -name coupler.res | wc -l)
[[ $written -eq $MEMBERS ]] || {
    echo "ensemble-ic: built $written of $MEMBERS member(s); not installing" >&2
    exit 1
}
rm -rf "$TARGET"
mkdir -p "$(dirname "$TARGET")"
mv "$WORK/ensemble" "$TARGET"
cp enspert.yaml "$TARGET/"

echo "ensemble-ic: wrote $MEMBERS member(s) to $TARGET"
echo "ensemble-ic: name it with  ensemble.initial_condition: $TARGET"
