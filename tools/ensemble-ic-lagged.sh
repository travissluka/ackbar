#!/bin/bash
# Build an ensemble initial condition from the same calendar date in other years.
#
#   tools/ensemble-ic-lagged.sh <domain> <control ic> <year>...
#   tools/ensemble-ic-lagged.sh gom_25km \
#       $ACKBAR_STATIC_ROOT/ic/gom_25km/osse-control-25km/20150712T00 \
#       $(seq 1993 2013) 2016
#
# Writes `<control ic>/lagged<N>/mem001..N/`, beside the state the members are
# an ensemble around, which is what `ensemble.initial_condition` names.
#
# The years are listed rather than given as a range because the useful set is
# never a range: the control's own year and the nature run's year both have to
# come out of the middle of it, and a sample has to be over-drawn because some
# members do not survive the settle that follows.
#
# ---------------------------------------------------------------------------
# Why this and not `tools/ensemble-ic.sh`
#
# That one draws each member from the static background error with
# `soca_enspert.x`, so the spread it produces is by construction the spread B
# claims the background has. It is the right starting point and the wrong
# ensemble: the perturbations carry B's correlation length scales and none of
# the ocean's flow structure, so the members have spread without dynamical
# balance, and B says nothing at all about velocity, so every member has an
# identical current field. An LETKF handed that ensemble has no velocity
# covariance to update velocity through.
#
# This draws each member from GLORYS on the *same calendar date in a different
# year*. Every member is a real ocean: balanced, with its own Loop Current
# position, its own rings and its own velocity field. Sampling one date across
# years rather than a span of dates within one year is what keeps the seasonal
# phase common to all of them, so the ensemble covariance carries mesoscale
# variability and not a summer stratification trend that recentring can only
# remove from the mean.
#
# ---------------------------------------------------------------------------
# Every member is integrated as if it were the control's year
#
# `fetch-glorys.py --valid-at` writes the GLORYS day it read into `source` and
# the day the state is *asserted* to estimate into `valid_at` and the time axis.
# So a member reads year Y's ocean and then integrates under the control year's
# open boundary and forcing, exactly like the control itself, which is a
# `--valid-at` product too. That matters twice: only one `obc.nc` is needed
# rather than one per year, and no member gets a boundary its neighbours did
# not, so the spread is interior initial state and nothing else.
#
# The 24 hour leg is not a spinup. It is the shortest run that makes MOM6 write
# a self-consistent restart set from a z-level initial condition, and it is
# where the interpolation shock is shed. See `coldstart-ic.sh`.
#
# ---------------------------------------------------------------------------
# It overwrites the domain's own `ic.nc`, one member at a time
#
# `fetch-glorys.py ic` writes `$ACKBAR_STATIC_ROOT/domain/<domain>/INPUT/ic.nc`,
# a single fixed path that is also what the model reads, so the loop below is
# serial and cannot be otherwise. The file in place when this starts is saved
# and put back at the end, including when the loop fails: a domain left holding
# the last member's initial condition would cold start every later experiment
# from year Y without saying so.
#
# **What it restores is whatever it found, which is not always what the domain
# should have.** Fetch an initial condition by hand to try something, then run
# this, and it faithfully puts the hand-fetched one back. Check
# `ncdump -h INPUT/ic.nc | grep valid_at` afterwards if anything touched it
# beforehand; the domain's own is the one the control was built from.
#
# ---------------------------------------------------------------------------
# This is the first of three steps, and its output is not the product
#
# `lagged<N>/` is the raw sample: N years of ocean, whose mean is a climatology
# of this date and not the control. Two steps follow it, and neither belongs
# here:
#
#     experiments/osse25-ensemble-settle.yaml   one free day, then
#     tools/ensemble-collect.sh                 renumber the survivors, then
#     tools/ensemble-recenter.py                move the mean onto the control
#
# **The recentring is last on purpose.** It used to run at the end of this
# script, before the settle, and that leaves the ensemble mean one free day
# ahead of the control rather than on it: measured on this domain, 0.40 degC in
# temperature and 0.07 m/s in velocity, the latter further off centre than the
# members are from each other. An ensemble filter cannot tell that offset from
# information and writes it into the control on the first cycle.
#
# The settle cannot move either, because its whole job is to shed the shock this
# script's 24 hour leg leaves behind, and a member has to be a state the model
# will integrate before it is worth recentring.
#
# `lagged<N>/` is kept rather than reaped: it is what the settle runs on, and
# each member records its year, so adding a year to the draw later re-downloads
# only the year that was added. It is derived data and safe to delete once the
# ensemble is in use.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

USAGE="usage: ensemble-ic-lagged.sh <domain> <control ic> <year>..."
DOMAIN=${1:?$USAGE}
CONTROL=${2:?$USAGE}
shift 2
[ $# -gt 0 ] || { echo "$USAGE" >&2; exit 1; }
YEARS=("$@")

[ -d "$CONTROL" ] || { echo "ensemble-ic-lagged: $CONTROL does not exist" >&2; exit 1; }

# The control's own stamp is the only date this needs. Members have to end
# valid at it, and the coldstart is 24 hours, so they start the day before and
# read GLORYS on that month and day in each year.
STAMP=$(basename "$CONTROL")                      # 20150712T00
TARGET=${STAMP:0:4}-${STAMP:4:2}-${STAMP:6:2}     # 2015-07-12
START=$(date -u -d "$TARGET - 1 day" +%Y-%m-%d)   # 2015-07-11
MONTHDAY=$(date -u -d "$START" +%m-%d)            # 07-11

# What the members were integrated over, so a domain whose sea floor has changed
# cannot reuse them. `cksum` rather than a modification time, because copying the
# domain in changes the latter and not the water.
TOPOGRAPHY=$(cksum "$ACKBAR_STATIC_ROOT/domain/$DOMAIN/INPUT/ocean_topog.nc" | cut -d" " -f1)

MEMBERS=${#YEARS[@]}
OUT=$CONTROL/lagged$MEMBERS
INPUT=$ACKBAR_STATIC_ROOT/domain/$DOMAIN/INPUT
SAVED=$INPUT/ic.nc.ensemble-ic-lagged.saved

echo "ensemble-ic-lagged: $MEMBERS member(s), GLORYS $MONTHDAY of ${YEARS[*]}"
echo "ensemble-ic-lagged: integrated $START -> $TARGET, written to $OUT"

# Put the domain back the way it was found, however this exits. The trap is set
# before the first overwrite so there is no window in which the saved copy
# exists and nothing would restore it.
restore() {
    if [ -e "$SAVED" ]; then
        mv -f "$SAVED" "$INPUT/ic.nc"
        echo "ensemble-ic-lagged: restored the domain's own ic.nc"
    fi
}
trap restore EXIT
cp -a "$INPUT/ic.nc" "$SAVED"

mkdir -p "$OUT"
MEMBER=0
for YEAR in "${YEARS[@]}"; do
    MEMBER=$((MEMBER + 1))
    NAME=$(printf "mem%03d" "$MEMBER")
    SLUG=lagged-$YEAR

    # The year a member already holds, if it holds one, *and* the sea floor it
    # was integrated over. Re-running this to add a year to a draw would
    # otherwise re-download every year already in it, and the download is the
    # only slow part; keying on the year alone would silently keep members built
    # over a bathymetry the domain no longer has, which is a restart whose layer
    # thicknesses do not sum to the column they claim.
    if [ "$(cat "$OUT/$NAME/year" 2>/dev/null)" = "$YEAR $TOPOGRAPHY" ]; then
        echo "ensemble-ic-lagged: $NAME is already GLORYS $YEAR-$MONTHDAY"
        continue
    fi

    echo "ensemble-ic-lagged: $NAME from GLORYS $YEAR-$MONTHDAY"
    env -u PYTHONPATH "$ACKBAR_ROOT/.venv-data/bin/python" \
        "$ACKBAR_ROOT/tools/fetch-glorys.py" ic "$DOMAIN" \
        "$YEAR-$MONTHDAY" --valid-at "$START" >/dev/null

    # Named for the year it came from rather than for the member index, because
    # the index is a position in this ensemble and the year is what the state
    # is. The member directory below is the position.
    "$ACKBAR_ROOT/tools/coldstart-ic.sh" "$DOMAIN" "${START}T00" 24 "$SLUG" >/dev/null

    rm -rf "$OUT/$NAME"
    cp -a "$ACKBAR_STATIC_ROOT/ic/$DOMAIN/$SLUG/$STAMP" "$OUT/$NAME"
    rm -rf "${ACKBAR_STATIC_ROOT:?}/ic/$DOMAIN/$SLUG"
    # Written last, so a member interrupted part way through is rebuilt rather
    # than believed. The restart itself records the GLORYS day it came from, but
    # not in anything cheap to read, and the member index is a position in this
    # ensemble rather than a year.
    echo "$YEAR $TOPOGRAPHY" > "$OUT/$NAME/year"
done

cat > "$OUT/README.md" <<EOF
# Lagged ensemble initial condition (the raw sample)

$MEMBERS members for \`$DOMAIN\`, valid at $TARGET, one per year: ${YEARS[*]}.
Each is GLORYS12V1 for $MONTHDAY of its own year, asserted to be an estimate of
$START and integrated 24 hours to $TARGET under this domain's own boundary and
forcing, exactly as the control beside this was.

Each member holds the year it was drawn from, and a checksum of the topography it
was integrated over, in \`year\`. The ensemble order is the order the years were
given, so \`mem001\` is ${YEARS[0]}.

**Neither settled nor recentred, and therefore not the one to run.** The mean
here is a $MEMBERS year climatology of $MONTHDAY, not the control, and every
member still carries the shock of being interpolated from a z-level field a day
ago. Two steps follow, in this order:

    ackbar create experiments/osse25-ensemble-settle.yaml
    ackbar start osse25-ensemble-settle
    tools/ensemble-collect.sh <that run's rst dir> $(basename "$OUT") <output dir> <count>
    tools/ensemble-recenter.py <output dir> $CONTROL

This directory is kept because the settle runs on it. It is derived data and
safe to delete once the ensemble is in use, and re-running the command below
reuses whatever of it is still here rather than downloading it again.

Rebuild with:

    tools/ensemble-ic-lagged.sh $DOMAIN $CONTROL ${YEARS[*]}
EOF

echo "ensemble-ic-lagged: wrote $MEMBERS member(s) to $OUT"
echo "ensemble-ic-lagged: settle them next, with experiments/osse25-ensemble-settle.yaml"
