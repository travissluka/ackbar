#!/bin/bash
# One free forecast per boundary member, from one shared initial condition.
#
#   tools/spike-obc.sh <domain> <ic-dir> <days> <out-dir> <obc-dir>
#
#   tools/spike-obc.sh gom_25km \
#       $ACKBAR_STATIC_ROOT/ic/gom_25km/osse-control-25km/20150712T00 \
#       30 /data/ackbar/spike/obc-lag21 /data/ackbar/spike/obc-lag21/obc
#
# `<obc-dir>` is what `tools/obc-lagged.py` wrote: `mem000.nc` onwards, one
# boundary per member. Each becomes that member's `INPUT/obc.nc` and nothing
# else about the run differs, so the spread between these forecasts is the
# boundary and only the boundary. Same restart set, same atmosphere, same
# parameters, same executable, and no stochastic physics: a member's own model
# noise would be indistinguishable from what the boundary did.
#
# `mem000` is the unperturbed boundary. It is a member of the spread rather than
# a control excluded from it, because with the anomaly form `obc-lagged.py`
# writes, the unperturbed boundary is the ensemble mean boundary. Its forecast
# is what every other member is a perturbation around.
#
# ---------------------------------------------------------------------------
# Why this is long where `spike-sweep.py` is five days
#
# A parameter perturbation acts everywhere at once and its spread is visible on
# day one. A boundary perturbation has to be advected in. Counillon and Bertino
# (2009) measured, on this basin, anomalies that appear first at the inflow
# boundary, travel around the Loop Current at about 30 km/day, and take roughly
# three weeks to mature. Five days would measure the boundary's own sponge zone
# and call it an ensemble.
#
# Thirty days is also the length of the OSSE experiments, so the last day here
# is the spread a 30 cycle experiment would have if its analysis never removed
# any, which is the upper bound worth knowing.
#
# ---------------------------------------------------------------------------
# Serial, and resumable
#
# One run at a time on every rank the site gives, because a member is a whole
# forecast and this machine has one node. Members already holding output are
# skipped, so a run interrupted halfway is restarted by issuing the same
# command: nothing here depends on the members having been run together.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)

DOMAIN=${1:?usage: spike-obc.sh <domain> <ic-dir> <days> <out-dir> <obc-dir>}
IC=${2:?usage: spike-obc.sh <domain> <ic-dir> <days> <out-dir> <obc-dir>}
DAYS=${3:?usage: spike-obc.sh <domain> <ic-dir> <days> <out-dir> <obc-dir>}
OUT=${4:?usage: spike-obc.sh <domain> <ic-dir> <days> <out-dir> <obc-dir>}
OBC=${5:?usage: spike-obc.sh <domain> <ic-dir> <days> <out-dir> <obc-dir>}

shopt -s nullglob
boundaries=("$OBC"/mem*.nc)
[[ ${#boundaries[@]} -ge 2 ]] || {
    echo "spike-obc: $OBC holds ${#boundaries[@]} mem*.nc files, which is not an ensemble" >&2
    echo "spike-obc: build one with tools/obc-lagged.py" >&2
    exit 1
}

echo "spike-obc: ${#boundaries[@]} members, $DAYS days each, from $IC"

# Artifact existence answers "did this member run", not "did it run the same
# experiment". Rerunning 30 days as 45, or against a different initial
# condition, into the same output directory would otherwise skip every member
# that already finished and leave an ensemble whose members are not comparable,
# which `spike-spread.py` reads without complaint because every member does have
# an `ocn_daily` file. So the settings that make members comparable are recorded
# beside them, and a run that disagrees stops rather than half-appending to
# someone else's ensemble.
SETTINGS="$OUT/spike-obc.settings"
WANT="domain=$DOMAIN days=$DAYS ic=$IC obc=$OBC"
mkdir -p "$OUT"
if [[ -f $SETTINGS ]]; then
    HAVE=$(<"$SETTINGS")
    if [[ $HAVE != "$WANT" ]]; then
        echo "spike-obc: $OUT already holds a different experiment" >&2
        echo "spike-obc:   it has  $HAVE" >&2
        echo "spike-obc:   you asked for $WANT" >&2
        echo "spike-obc: use a different output directory, or delete that one" >&2
        exit 1
    fi
else
    printf '%s' "$WANT" > "$SETTINGS"
fi

for path in "${boundaries[@]}"; do
    member=$(basename "$path" .nc)
    # Artifacts, not a marker file: the run is done when it has written the
    # output the spread is computed from, and nothing else counts as done.
    existing=("$OUT/$member"/*ocn_daily.nc)
    if [[ ${#existing[@]} -gt 0 ]]; then
        echo "spike-obc: $member already has output, skipping"
        continue
    fi
    echo "spike-obc: $member <- $(basename "$path")"
    SPIKE_LINK="obc.nc=$path" \
        "$ACKBAR_ROOT/tools/spike-forecast.sh" "$DOMAIN" "$IC" "$DAYS" "$OUT/$member"
done

echo "spike-obc: $OUT"
echo "spike-obc: tools/spike-spread.py --flat --root $OUT --json $OUT/spread.json"
