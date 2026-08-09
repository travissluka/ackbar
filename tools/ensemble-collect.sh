#!/bin/bash
# Collect the survivors of an ensemble settle into a numbered initial condition.
#
#   tools/ensemble-collect.sh <settle rst dir> <lagged dir> <output dir> <count>
#   tools/ensemble-collect.sh \
#       $ACKBAR_OUTPUT_ROOT/osse25-ensemble-settle/run/20150712T000000Z/rst \
#       $ACKBAR_STATIC_ROOT/ic/gom_25km/osse-control-25km/20150712T00/lagged22 \
#       $ACKBAR_STATIC_ROOT/ic/gom_25km/osse-control-25km/20150712T00/ensemble20-settled \
#       20
#
# The third step of the ensemble build, after `ensemble-ic-lagged.sh` draws the
# sample and `experiments/osse25-ensemble-settle.yaml` integrates it one free
# day. It exists as a tool rather than as a handful of `cp` and `awk` because it
# has been run by hand twice and both times something went in under the wrong
# number.
#
# ---------------------------------------------------------------------------
# Why there is a survivor list at all
#
# The settle is over-drawn: 22 members are integrated to produce an ensemble of
# 20. A member that MOM6 refuses to integrate freely for one day is not a sample
# of anything, and there is nothing in it to repair, so it is dropped and the
# next one takes its place. Which member that is has moved every time the
# recentring recipe changed, and every failure so far has been in a ten to
# twenty five metre column on the shelf, so the list is a property of this
# domain's sea floor rather than of any particular year's ocean.
#
# `mem000` is skipped: it is the control, which the ensemble is an ensemble
# *around* and not a member of.
#
# ---------------------------------------------------------------------------
# The states are restamped, and that is the assertion the ensemble rests on
#
# A settle cycle beginning at the control's own date ends a day after it, so
# every survivor's clock reads one day late. They are stamped back, exactly as
# `tools/restamp-ic.sh` stamps the control itself: the ensemble is a sample of
# the ocean around this control, and which day a member's water was integrated
# to is not what makes it one. The date is taken from the output directory's
# parent, which is the control's own stamp, so the two cannot disagree.
#
# `provenance.txt` maps each new number back to the member it came from and the
# GLORYS year that member was drawn from, because after renumbering there is
# otherwise no way to ask which years survived.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

USAGE="usage: ensemble-collect.sh <settle rst dir> <lagged dir> <output dir> <count>"
RST=${1:?$USAGE}
LAGGED=${2:?$USAGE}
OUT=${3:?$USAGE}
COUNT=${4:?$USAGE}

[ -d "$RST" ] || { echo "ensemble-collect: $RST does not exist" >&2; exit 1; }
# Checked for the same reason, and it is the one that used to be silent: every
# read of $LAGGED swallows its own error, so a wrong path here does not fail, it
# writes `GLORYS ?` into every line of the provenance file and then reports
# "Every member survived this settle" over an ensemble that lost members.
[ -d "$LAGGED" ] || { echo "ensemble-collect: $LAGGED does not exist (it is a path, not a name; ensemble-ic-lagged.sh prints a bare basename)" >&2; exit 1; }

# The control's stamp, which is the date the members are asserted at.
STAMP=$(basename "$(dirname "$OUT")")                       # 20150712T00
read -r YY MM DD HH <<< "$(date -u -d "${STAMP:0:4}-${STAMP:4:2}-${STAMP:6:2} ${STAMP:9:2}:00:00Z" '+%Y %-m %-d %-H')"

SURVIVORS=()
for MEMBER in "$RST"/mem*; do
    NAME=$(basename "$MEMBER")
    [ "$NAME" = mem000 ] && continue
    [ -f "$MEMBER/MOM.res.nc" ] || continue
    SURVIVORS+=("$NAME")
done

if [ "${#SURVIVORS[@]}" -lt "$COUNT" ]; then
    echo "ensemble-collect: ${#SURVIVORS[@]} member(s) survived the settle and $COUNT" \
         "were asked for; draw more years and settle again" >&2
    exit 1
fi

echo "ensemble-collect: ${#SURVIVORS[@]} survivor(s), taking $COUNT, stamped $STAMP"

rm -rf "$OUT.partial"
mkdir -p "$OUT.partial"
: > "$OUT.partial/provenance.txt"

INDEX=0
for NAME in "${SURVIVORS[@]:0:$COUNT}"; do
    INDEX=$((INDEX + 1))
    TO=$(printf "mem%03d" "$INDEX")
    YEAR=$(cat "$LAGGED/$NAME/year" 2>/dev/null || echo "?")

    cp -rL "$RST/$NAME" "$OUT.partial/$TO"
    awk -v y="$YY" -v m="$MM" -v d="$DD" -v h="$HH" '
        NR == 2 { printf "%6d%6d%6d%6d%6d%6d        Model start time:   year, month, day, hour, minute, second\n", y, m, d, h, 0, 0; next }
        NR == 3 { printf "%6d%6d%6d%6d%6d%6d        Current model time: year, month, day, hour, minute, second\n", y, m, d, h, 0, 0; next }
        { print }
    ' "$OUT.partial/$TO/coupler.res" > "$OUT.partial/$TO/coupler.res.new"
    [ -s "$OUT.partial/$TO/coupler.res.new" ] || {
        echo "ensemble-collect: the rewritten coupler.res for $TO is empty" >&2
        rm -rf "$OUT.partial"; exit 1; }
    mv "$OUT.partial/$TO/coupler.res.new" "$OUT.partial/$TO/coupler.res"
    rm -f "$OUT.partial/$TO/year"

    echo "$TO  <- $NAME  GLORYS $YEAR" >> "$OUT.partial/provenance.txt"
done

DROPPED=$(comm -13 <(printf '%s\n' "${SURVIVORS[@]}" | sort) \
                   <(ls "$LAGGED" | grep '^mem' | sort) | tr '\n' ' ')

cat > "$OUT.partial/README.md" <<EOF
# Settled ensemble sample, $COUNT members

The second of three products in the ensemble build, and **not the one to run**:
its mean is a climatology of this date rather than the control. Recentring it is
the last step.

1. \`tools/ensemble-ic-lagged.sh\` draws one GLORYS year per member and
   integrates each a day. -> \`../$(basename "$LAGGED")\`
2. \`experiments/osse25-ensemble-settle.yaml\` runs one free 24 hour forecast and
   \`tools/ensemble-collect.sh\` renumbers the survivors here.
3. \`tools/ensemble-recenter.py\` moves the mean onto the control, which is what
   an experiment names with \`ensemble.initial_condition\`.

\`provenance.txt\` maps each number back to the member it was drawn as and the
GLORYS year behind it.

**Why the settle exists, and why it is before the recentring.** A member arrives
from \`coldstart-ic.sh\` having been interpolated from a z-level field and
integrated the shortest run that writes a self-consistent restart set, which is
not long enough to shed the interpolation shock. Such a member integrates fine on
its own and dies in
\`MOM_regridding: adjust_interface_motion() - implied h<0\` when handed an
analysis increment smaller than what \`osse25-3dvar\` writes into the control
every cycle without trouble. A second free day gives it the room back. Settling
*after* the recentring instead leaves the mean a free day ahead of the control
rather than on it, which the filter writes into the control on the first cycle as
an increment it cannot tell from information.

**Why the draw is over-sized.** $(if [ -n "${DROPPED// /}" ]; then echo "The settle dropped ${DROPPED% }."; else echo "Every member survived this settle."; fi) A member the model
refuses to integrate freely is not a sample of anything and there is nothing in
it to repair, so the draw carries spares and the survivors are renumbered.

The states are valid one day after the free forecast began and are asserted to be
an estimate of this directory's date, exactly as \`tools/restamp-ic.sh\` means
it. Which day a member's water was integrated to is not what makes it a sample of
the ocean around this control.
EOF

rm -rf "$OUT"
mv "$OUT.partial" "$OUT"
echo "ensemble-collect: wrote $COUNT member(s) to $OUT"
