#!/bin/bash
# Build an ensemble initial condition from nearby calendar dates in other years.
#
#   tools/ensemble-ic-lagged.sh [--offsets "<days>..."] <domain> <control ic> <year>...
#   tools/ensemble-ic-lagged.sh gom_25km \
#       $ACKBAR_STATIC_ROOT/ic/gom_25km/osse-control-25km/20150712T00 \
#       $(seq 1993 2013) 2016
#   tools/ensemble-ic-lagged.sh --offsets "0 -5 5" gom_25km \
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
# The draw is years times offsets, because years alone run out
#
# GLORYS12V1 begins in 1993, and the control's own year and the nature run's
# have to be left out, so drawing one member per year has a hard ceiling in the
# high twenties that no amount of time buys past. `--offsets` multiplies the
# sample by taking each year at several nearby days: `--offsets "0 -5 5"` over
# 22 years is 66 draws where the years alone were 22.
#
# **A few days is not a season.** The paragraph below argues for sampling across
# years rather than across a span of dates within one year, and that argument is
# about seasonal phase: a member drawn in May and one drawn in September differ
# by the stratification cycle, which recentring can only take out of the mean.
# An offset of a few days moves the seasonal phase by a few days, which is
# nothing, while the mesoscale field decorrelates over a week or two, so the two
# draws are close to independent in exactly the part of the state this ensemble
# exists to sample. Keep the offsets inside a fortnight or the argument stops
# holding, and note that two draws five days apart in the same year are less
# independent than two different years: the sample grows faster than its rank.
#
# Offsets are applied outermost, so `mem001..N` are every year at the first
# offset. A draw truncated at the settle therefore degrades to the one-date
# ensemble rather than to an arbitrary corner of the outer product, and
# `--offsets "0"` is exactly the behaviour this script had before offsets
# existed.
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
# **The recentring is last on purpose.** Recentring before the settle leaves the
# ensemble mean one free day ahead of the control rather than on it, and an
# ensemble filter cannot tell that offset from information: it writes it into
# the control on the first cycle.
#
# The settle cannot move either, because its whole job is to shed the shock this
# script's 24 hour leg leaves behind, and a member has to be a state the model
# will integrate before it is worth recentring.
#
# `lagged<N>/` is kept rather than reaped: it is what the settle runs on, and
# each member records its year and the sea floor it was integrated over, so
# re-running this only redoes what actually changed. The fetched GLORYS fields
# are cached separately, under `ic/<domain>/glorys-ic-cache`, because they depend
# on the horizontal grid and not on the bathymetry: changing the sea floor
# invalidates every member's restart and none of its input, and re-drawing then
# costs seconds of cold start rather than an hour of downloads. Both are derived
# data and safe to delete once the ensemble is in use.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

USAGE="usage: ensemble-ic-lagged.sh [--offsets \"<days>...\"] <domain> <control ic> <year>..."

# Day offsets from the control's own month and day, one member per year per
# offset. Defaults to the single offset this script had before, so a command
# written against the old signature draws the same ensemble it always did.
OFFSETS=(0)
if [ "${1:-}" = "--offsets" ]; then
    [ $# -ge 2 ] || { echo "$USAGE" >&2; exit 1; }
    read -r -a OFFSETS <<< "$2"
    shift 2
fi
[ ${#OFFSETS[@]} -gt 0 ] || { echo "ensemble-ic-lagged: --offsets is empty" >&2; exit 1; }
for OFFSET in "${OFFSETS[@]}"; do
    case $OFFSET in
        0|-[0-9]*|[0-9]*) ;;
        *) echo "ensemble-ic-lagged: offset '$OFFSET' is not a whole number of days" >&2
           exit 1 ;;
    esac
done

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

# The draw, as one GLORYS date per member. Offsets outermost, so the first
# block of members is every year at the first offset; see the header for why
# that ordering is the one a truncated draw wants.
DRAWS=()
for OFFSET in "${OFFSETS[@]}"; do
    for YEAR in "${YEARS[@]}"; do
        # Resolved against the year being drawn from rather than the control's,
        # so every draw is the same distance from the same seasonal phase. `date`
        # refuses 02-29 in a common year, which is the one month and day this
        # cannot be run on without choosing offsets that avoid it.
        DRAWS+=("$(date -u -d "$YEAR-$MONTHDAY $OFFSET days" +%Y-%m-%d)")
    done
done

MEMBERS=${#DRAWS[@]}
OUT=$CONTROL/lagged$MEMBERS
INPUT=$ACKBAR_STATIC_ROOT/domain/$DOMAIN/INPUT
CACHE=$ACKBAR_STATIC_ROOT/ic/$DOMAIN/glorys-ic-cache
SAVED=$INPUT/ic.nc.ensemble-ic-lagged.saved

echo "ensemble-ic-lagged: $MEMBERS member(s), GLORYS $MONTHDAY of ${YEARS[*]}"
echo "ensemble-ic-lagged: offset ${OFFSETS[*]} day(s)"
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
for DRAW in "${DRAWS[@]}"; do
    MEMBER=$((MEMBER + 1))
    NAME=$(printf "mem%03d" "$MEMBER")
    SLUG=lagged-$DRAW

    # The GLORYS day a member already holds, if it holds one, *and* the sea floor
    # it was integrated over. Re-running this to add a draw would otherwise
    # re-download every day already in it, and the download is the only slow
    # part; keying on the day alone would silently keep members built over a
    # bathymetry the domain no longer has, which is a restart whose layer
    # thicknesses do not sum to the column they claim.
    #
    # The whole date rather than the year, because with offsets the year no
    # longer identifies the draw: 1997 at +5 days and 1997 at -5 days are two
    # members and share a year.
    if [ "$(cat "$OUT/$NAME/year" 2>/dev/null)" = "$DRAW $TOPOGRAPHY" ]; then
        echo "ensemble-ic-lagged: $NAME is already GLORYS $DRAW"
        continue
    fi

    # The fetched field is cached, because it does not depend on the sea floor
    # and re-drawing the ensemble usually does. `fetch-glorys.py ic` reads
    # `ocean_hgrid.nc` and never `ocean_topog.nc`, so a smoothing change
    # invalidates every member's *restart* and none of its input, and the
    # download is minutes where the cold start that follows is seconds.
    CACHED=$CACHE/$DRAW-valid-$START.nc
    if [ -s "$CACHED" ]; then
        echo "ensemble-ic-lagged: $NAME from GLORYS $DRAW (cached)"
        cp -a "$CACHED" "$INPUT/ic.nc"
    else
        echo "ensemble-ic-lagged: $NAME from GLORYS $DRAW"
        env -u PYTHONPATH "$ACKBAR_ROOT/.venv-data/bin/python" \
            "$ACKBAR_ROOT/tools/fetch-glorys.py" ic "$DOMAIN" \
            "$DRAW" --valid-at "$START" >/dev/null
        mkdir -p "$CACHE"
        # Written under a temporary name and moved, so an interrupted copy is
        # not a cache entry that looks complete.
        cp -a "$INPUT/ic.nc" "$CACHED.partial" && mv "$CACHED.partial" "$CACHED"
    fi

    # Named for the day it came from rather than for the member index, because
    # the index is a position in this ensemble and the day is what the state
    # is. The member directory below is the position.
    "$ACKBAR_ROOT/tools/coldstart-ic.sh" "$DOMAIN" "${START}T00" 24 "$SLUG" >/dev/null

    rm -rf "$OUT/$NAME"
    cp -a "$ACKBAR_STATIC_ROOT/ic/$DOMAIN/$SLUG/$STAMP" "$OUT/$NAME"
    rm -rf "${ACKBAR_STATIC_ROOT:?}/ic/$DOMAIN/$SLUG"
    # Written last, so a member interrupted part way through is rebuilt rather
    # than believed. The restart itself records the GLORYS day it came from, but
    # not in anything cheap to read, and the member index is a position in this
    # ensemble rather than a day.
    echo "$DRAW $TOPOGRAPHY" > "$OUT/$NAME/year"
done

cat > "$OUT/README.md" <<EOF
# Lagged ensemble initial condition (the raw sample)

$MEMBERS members for \`$DOMAIN\`, valid at $TARGET, drawn from ${#YEARS[@]} year(s)
(${YEARS[*]}) at ${#OFFSETS[@]} offset(s) in days from $MONTHDAY (${OFFSETS[*]}).
Each is GLORYS12V1 for its own day, asserted to be an estimate of
$START and integrated 24 hours to $TARGET under this domain's own boundary and
forcing, exactly as the control beside this was.

Each member holds the GLORYS day it was drawn from, and a checksum of the
topography it was integrated over, in \`year\`. Offsets vary slowest, so
\`mem001\` is ${DRAWS[0]} and the first ${#YEARS[@]} members are the
offset ${OFFSETS[0]} draw.

**Neither settled nor recentred, and therefore not the one to run.** The mean
here is a climatology of ${#YEARS[@]} years around $MONTHDAY, not the control, and every
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

    tools/ensemble-ic-lagged.sh --offsets "${OFFSETS[*]}" $DOMAIN $CONTROL ${YEARS[*]}
EOF

echo "ensemble-ic-lagged: wrote $MEMBERS member(s) to $OUT"
echo "ensemble-ic-lagged: settle them next, with experiments/osse25-ensemble-settle.yaml"
