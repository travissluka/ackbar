#!/bin/bash
# Promote a model state to the initial condition stage, valid at a chosen date.
#
#   tools/restamp-ic.sh <state dir> <domain> <slug> <YYYY-MM-DDThh>
#   tools/restamp-ic.sh $ACKBAR_STATIC_ROOT/truth/gom_25km/osse-2015/20150825T0000 \
#                       gom_25km osse-control 2015-07-14T00
#
# Writes $ACKBAR_STATIC_ROOT/ic/<domain>/<slug>/<YYYYmmddThh>, which is what an
# experiment's `model.initial_condition` names.
#
# **This is the time-lagged initial condition, and the whole tool is one line of
# `coupler.res`.** A restart set carries its own clock, and once
# `INPUT/coupler.res` exists MOM6 takes the date from there rather than from the
# namelist, so handing an experiment a state stamped with a different date makes
# it integrate from that date: the wrong forcing, the wrong open boundary, and a
# restart written under a date nothing else in the experiment agrees with.
#
# Which is why the *point* of an OSSE control is exactly that: a truth state
# from one date, asserted to be an estimate of the ocean at another. The
# difference between the two is the initial error, and it is the only reason
# there is anything for an analysis to correct. Restamping is how that assertion
# gets made in a way the model will honour.
#
# Passing a state its own date is the other use, and it is not a degenerate one:
# it promotes a run's final state out of the `run/` tree that cleanup prunes, so
# the next stage names a stable path instead of reaching into another
# experiment's disposable output. The README says which of the two happened,
# because on disk they are the same directory.
#
# The date is an argument rather than something derived, for the same reason it
# is in `coldstart-ic.sh`: promoting a state to a date is the decision this
# stage exists to record, and getting it wrong produces a state that is
# internally consistent and simply not when it says it is.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

SOURCE=${1:?usage: restamp-ic.sh <state dir> <domain> <slug> <YYYY-MM-DDThh>}
DOMAIN=${2:?usage: restamp-ic.sh <state dir> <domain> <slug> <YYYY-MM-DDThh>}
SLUG=${3:?usage: restamp-ic.sh <state dir> <domain> <slug> <YYYY-MM-DDThh>}
WHEN=${4:?usage: restamp-ic.sh <state dir> <domain> <slug> <YYYY-MM-DDThh>}

: "${ACKBAR_STATIC_ROOT:?the site did not set ACKBAR_STATIC_ROOT}"

[[ -f $SOURCE/MOM.res.nc ]] || {
    echo "restamp-ic: $SOURCE holds no MOM.res.nc, so it is not a restart set" >&2
    exit 1
}
[[ -f $SOURCE/coupler.res ]] || {
    echo "restamp-ic: $SOURCE holds no coupler.res, so there is no clock to restamp" >&2
    exit 1
}

STAMP=$(date -u -d "${WHEN}:00:00Z" +%Y%m%dT%H)
OUT=$ACKBAR_STATIC_ROOT/ic/$DOMAIN/$SLUG/$STAMP
[[ -e $OUT ]] && { echo "restamp-ic: $OUT already exists; remove it to rebuild" >&2; exit 1; }

WAS=$(awk 'NR==3 {printf "%04d-%02d-%02dT%02d", $1, $2, $3, $4}' "$SOURCE/coupler.res")

mkdir -p "$(dirname "$OUT")"

# `-L` dereferences, and it is load bearing twice over. A state can be a
# directory of symlinks (a materialized initial condition is), so without it
# this would copy the links; and the rewrite below redirects into
# `coupler.res`, which through a link truncates whatever it points at.
rm -rf "$OUT.partial"
cp -rL "$SOURCE" "$OUT.partial"

# Both the start time and the current time, because the two together are what
# FMS reports as the run's elapsed clock, and a start time later than the
# current one is what makes it report a negative one.
#
# Written beside the file and moved over it rather than redirected into it, so
# that the source is fully read before anything is truncated even if the two
# ever turn out to be the same file.
read -r YY MM DD HH <<< "$(date -u -d "${WHEN}:00:00Z" '+%Y %-m %-d %-H')"
awk -v y="$YY" -v m="$MM" -v d="$DD" -v h="$HH" '
    NR == 2 { printf "%6d%6d%6d%6d%6d%6d        Model start time:   year, month, day, hour, minute, second\n", y, m, d, h, 0, 0; next }
    NR == 3 { printf "%6d%6d%6d%6d%6d%6d        Current model time: year, month, day, hour, minute, second\n", y, m, d, h, 0, 0; next }
    { print }
' "$OUT.partial/coupler.res" > "$OUT.partial/coupler.res.new"

[[ -s $OUT.partial/coupler.res.new ]] || {
    echo "restamp-ic: the rewritten coupler.res is empty; refusing to install it" >&2
    rm -rf "$OUT.partial"
    exit 1
}
mv "$OUT.partial/coupler.res.new" "$OUT.partial/coupler.res"
mv "$OUT.partial" "$OUT"

# The README has to say which of the two things this state is, because they
# read identically on disk and mean opposite things. A state promoted to its
# own valid time is a handoff between stages; a state promoted to any other is
# the asserted initial error an OSSE control exists to carry. Claiming the
# second when it was the first tells the next reader an offset is deliberate
# when there is no offset at all.
if [[ $WAS == "$WHEN" ]]; then
    CLOCK="whose own clock already read ${WHEN}:00:00Z, so nothing was restamped:
tools/restamp-ic.sh made the copy and wrote this file. Every byte, coupler.res
included, matches the source."
    MEANING="**There is no offset, and that is the point.** This is a handoff
between stages: one run's final state, promoted out of the disposable run
directory so that the next stage names a stable path rather than reaching inside
a tree cleanup prunes. It is the ocean at $STAMP as the model that produced it
had it."
else
    CLOCK="whose own clock read ${WAS}, restamped to ${WHEN}:00:00Z by
tools/restamp-ic.sh. The fields are unchanged; only coupler.res differs."
    MEANING="**The offset is deliberate and it is the initial error.** A state of
the ocean from one date, used as an estimate of the ocean at another, which is
how an OSSE gets a control that is wrong in a realistic way rather than wrong by
added noise. It is not an error and it is not a state anyone should read as being
the ocean at $STAMP."
fi

cat > "$(dirname "$OUT")/README" <<EOF
$DOMAIN initial conditions, $SLUG. One state per directory, named for the date
it is asserted to be valid at.

$STAMP is a copy of

    $SOURCE

$CLOCK

$MEANING

Regenerate with:
    tools/restamp-ic.sh $SOURCE $DOMAIN $SLUG $WHEN
EOF

echo "restamp-ic: $SOURCE ($WAS) -> $OUT"
head -3 "$OUT/coupler.res"
