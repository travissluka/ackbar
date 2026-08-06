#!/bin/bash
# Import one resolution of the Gulf of Mexico MOM6-SIS2 configuration.
#
#   tools/import-gom-domain.sh <source-dir> <domain>
#   tools/import-gom-domain.sh /data/OLD/work_old/mom6-gom-config/04deg gom_25km
#
# The import lands in two places, and which half goes where is the whole point:
#
#   config/model/mom6sis2/domain/gom/<res>/      its text, in git
#   $ACKBAR_STATIC_ROOT/domain/<domain>/INPUT/   its data, not in git
#
# That is MOM6-examples' own convention. Its cases track `MOM_input`, the
# tables and `input.nml`, while `INPUT/` holds symlinks into a `.datasets`
# directory that is gitignored and machine-local. Text decides the answer and
# belongs under review; a 300 MB grid file does not belong in a clone.
#
# The text arrives **verbatim and whole**, which is not where it ends up. The
# Gulf resolutions share `domain/gom/common`, and reducing a fresh import to the
# handful of parameters that are actually about its resolution is a reading job,
# not something this can do: the differences it would have to classify are what
# `gom/common` exists to stop accumulating. So this drops the full set beside
# the other resolutions' overrides and says so, and someone diffs it.
#
# ACKBAR deviates from upstream on one detail. Upstream joins the two halves
# with a relative symlink through `.datasets`; here the domain layer names the
# data directory as an absolute path (`mom6_input_dir`). A dangling symlink can
# only be discovered by the job that fails on it, deep inside FMS, while an
# absolute path is stat'd by `ackbar validate` before anything is submitted.
# Same reasoning that moved `namelist` and `fields metadata` off relative paths.
#
# Nothing here edits MOM_input, because what ACKBAR changes is supposed to be
# one file you can read on its own: MOM_override, written beside it and not by
# this script. An import that silently fixed things would put those changes
# beyond reading.
#
# The atmospheric forcing is not copied per domain. It is a global NCAR
# climatology, identical at every resolution and 911 MB, so it goes to the
# forcing stage once and each domain links to it. That is also where a real
# reanalysis lands when the climatology is replaced, which is why it is a stage
# and not a directory inside one domain's data.
#
# The vertical coordinate files (hycom1_*, layer_coord_*) are copied in rather
# than shared, despite being small and identical today. They define a domain's
# own NK, and two domains that differ only in vertical coordinate must not be
# able to drift onto one file.
#
# The source directories are named in fractions of a degree, and the digits are
# ambiguous: `025deg` is 1/25 degree, not a quarter degree. docs/domains.md has
# the mapping. Do not infer it from the directory name.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

SOURCE=${1:?usage: import-gom-domain.sh <source-dir> <domain>}
DOMAIN=${2:?usage: import-gom-domain.sh <source-dir> <domain>}

: "${ACKBAR_STATIC_ROOT:?the site did not set ACKBAR_STATIC_ROOT}"
TEXT=$ACKBAR_ROOT/config/model/mom6sis2/domain/gom/${DOMAIN#gom_}
DATA=$ACKBAR_STATIC_ROOT/domain/$DOMAIN/INPUT
FORCING=$ACKBAR_STATIC_ROOT/forcing/ncar-clim

#: The text half, which is what version control is for.
TEXT_FILES=(data_table field_table input.nml MOM_input SIS_input)

#: Files that are the shared climatology rather than this domain's own.
FORCED=(ncar_precip.clim.nc ncar_rad.clim.nc q_10_mod.clim.nc
        t_10_mod.clim.nc u_10_mod.clim.nc v_10_mod.clim.nc)

for name in "${TEXT_FILES[@]}"; do
    [[ -e $SOURCE/$name ]] || { echo "import-gom-domain: $SOURCE/$name is missing, so $SOURCE is not a MOM6-SIS2 configuration" >&2; exit 1; }
done
[[ -d $SOURCE/INPUT ]] || { echo "import-gom-domain: $SOURCE/INPUT does not exist" >&2; exit 1; }
[[ -e $DATA ]] && { echo "import-gom-domain: $DATA already exists; remove it to reimport" >&2; exit 1; }

# --- the shared forcing stage, once ------------------------------------------
mkdir -p "$FORCING"
for name in "${FORCED[@]}"; do
    [[ -e $FORCING/$name ]] && continue
    [[ -e $SOURCE/INPUT/$name ]] || continue
    echo "import-gom-domain: staging forcing $name"
    cp -L "$SOURCE/INPUT/$name" "$FORCING/$name.partial"
    mv "$FORCING/$name.partial" "$FORCING/$name"
done
if [[ ! -e $FORCING/README ]]; then
    cat > "$FORCING/README" <<'EOF'
NCAR/CORE climatological atmospheric forcing, as used by the Gulf of Mexico
MOM6-SIS2 cases. A climatology, so it has no period: every cycle of every year
reads the same annual cycle.

This is a placeholder for a real product. Being a climatology it carries no
synoptic variability at all, which caps how realistic any run on this domain
can be and means forecast error is dominated by missing weather rather than by
anything an analysis can correct. Replacing it with ERA5, JRA-55 or GEFS is
tracked work; when that happens the replacement lands beside this directory
under its own source name rather than overwriting it.
EOF
fi

# --- the data ----------------------------------------------------------------
echo "import-gom-domain: importing data -> $DATA"
mkdir -p "$DATA.partial"
shared() { local n=$1; for f in "${FORCED[@]}"; do [[ $f == "$n" ]] && return 0; done; return 1; }
for path in "$SOURCE"/INPUT/*; do
    name=$(basename "$path")
    if shared "$name"; then
        ln -s "$FORCING/$name" "$DATA.partial/$name"
    else
        cp -L "$path" "$DATA.partial/$name"
    fi
done
mkdir -p "$(dirname "$DATA")"
mv "$DATA.partial" "$DATA"

cat > "$(dirname "$DATA")/README" <<EOF
Data half of the MOM6-SIS2 configuration for the $DOMAIN domain: grid,
bathymetry, initial condition and open boundary conditions, imported from
$SOURCE by tools/import-gom-domain.sh.

The text half is in the ackbar repository: the parameters the Gulf resolutions
share are in config/model/mom6sis2/domain/gom/common, and what makes this one
this resolution is in domain/gom/${DOMAIN#gom_}. Neither half is usable alone.

The atmospheric forcing here is symlinked to the shared forcing stage, which
has its own README.

Known limits of this configuration, in particular the frozen single-snapshot
open boundary and the climatological atmosphere, are in docs/domains.md.
EOF

# --- the text ----------------------------------------------------------------
echo "import-gom-domain: importing text -> $TEXT"
mkdir -p "$TEXT"
for name in "${TEXT_FILES[@]}"; do
    cp -L "$SOURCE/$name" "$TEXT/$name"
done

echo "import-gom-domain: wrote $TEXT and $DATA"
du -sh "$DATA"
echo
echo "still to do by hand:"
echo "  * reduce $TEXT against domain/gom/common: it holds the whole imported"
echo "    configuration, and only what is about this resolution should stay"
echo "  * $TEXT/MOM_override, SIS_override and MOM_override.soca"
echo "  * a domain layer naming mom6_base_dir, mom6_input_dir and mom6_override_dir"
echo "  * tools/soca-gridspec.sh $DOMAIN"
echo "  * git add $TEXT"
