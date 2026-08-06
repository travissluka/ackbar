#!/bin/bash
# Build a domain's SOCA geometry: the static stage's one product so far.
#
#   tools/soca-gridspec.sh <mom6-case> [<output-dir>]
#   tools/soca-gridspec.sh OM_1deg
#
# Writes `soca_gridspec.nc` into $ACKBAR_STATIC_ROOT/static/<domain>, which is
# what the domain layer's `static:` names. Keyed on domain and nothing else, so
# every experiment on the domain reads the same file and comparing two of them
# is not conditional on how each was set up.
#
# Offline on purpose. This runs a full MOM6 initialization to get the grid,
# which wants more memory than the analysis it serves, and it produces the same
# bytes every time. A cycling job that did it would pay that per cycle, per
# member, to recompute a constant.
#
# Two things it needs that are not obvious:
#
#   * a `diag_table` in the working directory, even though nothing here writes a
#     diagnostic. FMS reads one during `initialize_MOM` and its absence is fatal
#     inside the geometry constructor, which surfaces as a segfault rather than
#     as a message about a missing file.
#
#   * a namelist that is *not* called `input.nml`. SOCA copies the one it is
#     given to that name in the working directory and asserts the source was
#     something else.
#
# Rerun it after a bundle bump that touches MOM6 or the SOCA geometry, and after
# any change to the case's grid. Nothing detects a stale gridspec: it is a file
# whose date is older than the grid it describes.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

CASE=${1:?usage: soca-gridspec.sh <mom6-case> [<output-dir>]}
CASE_DIR=$ACKBAR_ROOT/pkg/mom6sis2/ice_ocean_SIS2/$CASE
DOMAIN=$(echo "$CASE" | tr '[:upper:]' '[:lower:]')
OUT=${2:-$ACKBAR_STATIC_ROOT/static/$DOMAIN}

GRIDGEN=$ACKBAR_ROOT/pkg/jedi/build/bin/soca_gridgen.x
NAMELIST=$ACKBAR_ROOT/config/model/mom6sis2/mom_input.nml
METADATA=$ACKBAR_ROOT/config/model/mom6sis2/fields_metadata.yaml
# Rossby radius, which becomes a field in the gridspec and is read by the
# horizontal localization the ensemble phases configure. Not needed by hofx.
ROSSBY=$ACKBAR_ROOT/pkg/jedi/soca/test/Data/rossrad.nc

for path in "$GRIDGEN" "$NAMELIST" "$METADATA" "$CASE_DIR/MOM_input"; do
    [[ -e $path ]] || { echo "soca-gridspec: $path does not exist" >&2; exit 1; }
done

WORK=$(mktemp -d "${TMPDIR:-/tmp}/soca-gridspec.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

ln -s "$CASE_DIR/INPUT" INPUT
cp "$CASE_DIR/MOM_input" "$CASE_DIR/MOM_override" .
cp "$NAMELIST" mom_input.nml
printf 'soca_gridspec\n1 1 1 0 0 0\n' > diag_table

{
    echo "geometry:"
    echo "  geom_grid_file: soca_gridspec.nc"
    echo "  mom6_input_nml: mom_input.nml"
    echo "  fields metadata: $METADATA"
    [[ -e $ROSSBY ]] && echo "  rossby file: $ROSSBY"
} > gridgen.yml

echo "soca-gridspec: building $CASE geometry in $WORK"
mpiexec -n 1 "$GRIDGEN" gridgen.yml

[[ -s soca_gridspec.nc ]] || {
    echo "soca-gridspec: $GRIDGEN exited 0 and wrote no gridspec" >&2
    exit 1
}

mkdir -p "$OUT"
mv soca_gridspec.nc "$OUT/soca_gridspec.nc"
echo "soca-gridspec: wrote $OUT/soca_gridspec.nc"
