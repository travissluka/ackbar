#!/bin/bash
# Build a domain's SOCA geometry: the static stage's one product so far.
#
#   tools/soca-gridspec.sh <domain> [<output-dir>]
#   tools/soca-gridspec.sh gom_25km
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
# Three things it needs that are not obvious:
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
#   * on a regional domain, `MOM_override.soca`, which switches the open
#     boundaries off. SOCA's MOM6 is built without symmetric memory and refuses
#     to configure Flather boundaries at all, so without it this aborts in the
#     geometry constructor with a message about symmetric memory. The grid is the
#     same grid either way. See docs/domains.md.
#
# Rerun it after a bundle bump that touches MOM6 or the SOCA geometry, and after
# any change to the domain's grid. Nothing detects a stale gridspec: it is a file
# whose date is older than the grid it describes.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

DOMAIN=${1:?usage: soca-gridspec.sh <domain> [<output-dir>]}
OUT=${2:-$ACKBAR_STATIC_ROOT/static/$DOMAIN}

# The three parts of a domain's configuration, read out of the domain layer so
# that this builds the geometry from the same files the model will integrate on.
source "$ACKBAR_ROOT/tools/domain-paths.sh"
domain_paths "$DOMAIN"

GRIDGEN=$ACKBAR_ROOT/pkg/jedi/build/bin/soca_gridgen.x
NAMELIST=$ACKBAR_ROOT/config/model/mom6sis2/mom_input.nml
METADATA=$ACKBAR_ROOT/config/model/mom6sis2/fields_metadata.yaml
# Rossby radius, which becomes a field in the gridspec. It is the most
# load-bearing science input this script has: `config/static/diffusion.yaml`
# scales it into the `hz`, `hz_ssh` and `loc_hz` correlation lengths, and the
# LETKF's Rossby localization reads it at every grid point. Checked with the
# rest rather than treated as optional, because SOCA reads `rossby file` with no
# default: a gridspec built without it fails much later and twice over, as a
# missing key when the diffusion scales are computed and as a garbage
# localization radius after that.
#
# Still a path into a submodule's test tree, which `git clean` there removes and
# which the convention followed by `fields_metadata.yaml` and `obsop_name_map.yml`
# says not to depend on. Where it should live instead is a question about the
# static stage rather than about this script.
ROSSBY=$ACKBAR_ROOT/pkg/jedi/soca/test/Data/rossrad.nc

for path in "$GRIDGEN" "$NAMELIST" "$METADATA" "$ROSSBY" "$BASE/MOM_input" \
            "$OVERRIDE/MOM_override" "$OVERRIDE/MOM_override.soca" "$DATA"; do
    [[ -e $path ]] || { echo "soca-gridspec: $path does not exist" >&2; exit 1; }
done

WORK=$(mktemp -d "${TMPDIR:-/tmp}/soca-gridspec.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

ln -s "$DATA" INPUT
cp "$BASE/MOM_input" .
# ACKBAR's override, not just the stock configuration, and for the same reason
# the analysis reads it: the geometry this writes has to be the geometry the
# model integrates on, and a parameter that moves the grid must move both or
# neither.
cp "$OVERRIDE/MOM_override" MOM_override
# And the SOCA-only override, which on a regional domain is what stops MOM6
# refusing to configure Flather boundaries in its non-symmetric build. Read it.
cp "$OVERRIDE/MOM_override.soca" MOM_override.soca
cp "$NAMELIST" mom_input.nml
printf 'soca_gridspec\n1 1 1 0 0 0\n' > diag_table

{
    echo "geometry:"
    echo "  geom_grid_file: soca_gridspec.nc"
    echo "  mom6_input_nml: mom_input.nml"
    echo "  fields metadata: $METADATA"
    echo "  rossby file: $ROSSBY"
} > gridgen.yml

echo "soca-gridspec: building $DOMAIN geometry in $WORK"
mpiexec -n 1 "$GRIDGEN" gridgen.yml

[[ -s soca_gridspec.nc ]] || {
    echo "soca-gridspec: $GRIDGEN exited 0 and wrote no gridspec" >&2
    exit 1
}

# The staggered fields as generated describe the *east* and *north* faces,
# because that is the only face set a non-symmetric MOM6 has an index for, and
# on the domains where it can be done they are moved onto the west and south
# faces, which are the ones SOCA's reader actually loads out of a symmetric
# restart. Without this every velocity in the system is masked and labelled one
# cell away from where it is. The reasoning, the evidence and how to re-check it
# are in `ackbar.gridspec`.
#
# Here rather than inside the reader because the reader cannot be told which
# columns to take: `commit_reader_strided` starts at the tracer origin for every
# variable. So the gridspec has to describe the columns it takes.
#
# Either way the file records which face set it ended up on. The domain layer
# decides, and it is read rather than inferred from the grid: a domain that
# cannot take the shift and a domain whose land mask is wrong at the boundary
# look identical from inside this script, and guessing between them is how a
# regional domain would come to skip the shift silently.
case $STAGGERED in
    west/south)
        python -c "from ackbar.gridspec import shift_staggered; \
shift_staggered('soca_gridspec.nc')"
        ;;
    east/north)
        echo "soca-gridspec: $DOMAIN declares east/north, recording without shifting"
        python -c "from ackbar.gridspec import record_generated; \
record_generated('soca_gridspec.nc')"
        ;;
    *)
        echo "soca-gridspec: $DOMAIN declares staggered_faces=$STAGGERED, which is neither west/south nor east/north" >&2
        exit 1
        ;;
esac

mkdir -p "$OUT"
mv soca_gridspec.nc "$OUT/soca_gridspec.nc"
echo "soca-gridspec: wrote $OUT/soca_gridspec.nc"
