#!/bin/bash
# All three spread sources at once: a GEFS member, an oSPPT pattern, and a draw
# of perturbed parameters, one combination per ensemble member.
#
#   tools/spike-combined.sh /data/ackbar/spike/combined
#
# The parameter draw is fixed per member and the same in every cycle a member
# would run, which is the perturbed-parameter half. The oSPPT seed is fixed per
# member too, but the pattern it generates varies continuously in time, which is
# the stochastic half. The GEFS member supplies the third.
#
# Values are drawn from the four parameters the sweep found strongest, each
# member taking a different point in each range rather than a random sample.
# Five members cannot sample a four dimensional space, so a Latin-square-like
# spread of the ranges says more than five draws from them would.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

OUT=${1:?usage: spike-combined.sh <out-dir>}
GEFS=${GEFS:-/data/ackbar/spike/gefs}
IC=${IC:-$ACKBAR_STATIC_ROOT/ic/gom_25km/osse-control-25km/20150712T00}
DAYS=${DAYS:-5}
EXE=${SPIKE_EXE:-/data/ackbar/spike/build-stoch/coupler_main}
TABLE=$ACKBAR_ROOT/config/model/mom6sis2/domain/gom/common/data_table.gefs

[[ -x $EXE ]] || { echo "spike-combined: $EXE does not exist; run tools/spike-build-stochastic.sh" >&2; exit 1; }

MEMBERS=(c00 p01 p02 p03 p04)
SEEDS=(101 202 303 404 505)
# KV, KD, RINO_CRIT, MSTAR: the four the parameter sweep ranked highest, each
# spanning its own defensible range across the five members.
KV=(3.0E-05 1.0E-04 3.0E-04 1.0E-05 1.0E-03)
KD=(7.0E-06 1.5E-05 3.0E-05 7.0E-05 3.0E-06)
RINO=(0.35 0.25 0.15 0.50 0.20)
MSTAR=(1.7 1.2 0.9 2.4 0.6)

mkdir -p "$OUT/namelists"
for n in "${!MEMBERS[@]}"; do
    member=${MEMBERS[$n]}
    target=$OUT/mem$n
    if compgen -G "$target/*ocn_daily.nc" > /dev/null; then
        echo "have mem$n"
        continue
    fi
    namelist=$OUT/namelists/nam_stochy.mem$n
    cat > "$namelist" <<EOF
&nam_stochy
  ocnsppt = 0.4
  ocnsppt_lscale = 500000
  ocnsppt_tau = 21600
  iseed_ocnsppt = ${SEEDS[$n]}
  epbl = 0.5
  epbl_lscale = 500000
  epbl_tau = 21600
  iseed_epbl = ${SEEDS[$n]}
/
EOF
    SPIKE_EXE=$EXE \
    SPIKE_DATA_TABLE=$TABLE \
    SPIKE_LINK="atm.nc=$GEFS/$member/atm.nc" \
    SPIKE_NAMELIST=$namelist \
    NTASKS=${NTASKS:-8} \
        bash "$ACKBAR_ROOT/tools/spike-forecast.sh" gom_25km "$IC" "$DAYS" "$target" \
            DO_SPPT=True PERT_EPBL=True \
            KV="${KV[$n]}" KD="${KD[$n]}" \
            RINO_CRIT="${RINO[$n]}" MSTAR="${MSTAR[$n]}"
done

echo "spike-combined: $OUT"
