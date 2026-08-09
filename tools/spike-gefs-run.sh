#!/bin/bash
# Five forecasts from one ocean state, each forced by a different GEFS member.
#
#   tools/spike-gefs-run.sh /data/ackbar/spike/gefs /data/ackbar/spike/forcing
#
# The only thing that differs between members is the atmosphere, so the spread
# this produces is the ocean's response to atmospheric uncertainty alone, and is
# directly comparable to a parameter group from `tools/spike-sweep.py`: same
# initial condition, same length, same reduction.
#
# It is not comparable to the *experiment's* forcing. Every member here reads
# GEFS where a cycling experiment reads an NCAR normal-year climatology, so the
# mean state drifts differently. That changes what all five members do together
# and not how far apart they get, which is what is being measured.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

GEFS=${1:?usage: spike-gefs-run.sh <gefs-dir> <out-dir>}
OUT=${2:?usage: spike-gefs-run.sh <gefs-dir> <out-dir>}
IC=${IC:-$ACKBAR_STATIC_ROOT/ic/gom_25km/osse-control-25km/20150712T00}
DAYS=${DAYS:-5}

TABLE=$ACKBAR_ROOT/config/model/mom6sis2/domain/gom/common/data_table.gefs
[[ -e $TABLE ]] || { echo "spike-gefs-run: $TABLE does not exist" >&2; exit 1; }

for member in c00 p01 p02 p03 p04; do
    atm=$GEFS/$member/atm.nc
    [[ -s $atm ]] || {
        echo "spike-gefs-run: $atm does not exist; run tools/spike-gefs-atm.py" >&2
        exit 1
    }
    if compgen -G "$OUT/$member/*ocn_daily.nc" > /dev/null; then
        echo "have $member"
        continue
    fi
    SPIKE_DATA_TABLE=$TABLE SPIKE_LINK="atm.nc=$atm" \
        bash "$ACKBAR_ROOT/tools/spike-forecast.sh" gom_25km "$IC" "$DAYS" "$OUT/$member"
done

echo "spike-gefs-run: $OUT"
