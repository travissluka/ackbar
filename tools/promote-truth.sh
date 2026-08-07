#!/bin/bash
# Promote a free run's states to a read-only truth archive.
#
#   tools/promote-truth.sh <experiment> <archive-name>
#   tools/promote-truth.sh osse-truth osse-2015
#
# Writes $ACKBAR_STATIC_ROOT/truth/<domain>/<archive-name>/, holding
#
#   <YYYYmmddTHHMM>.nc    one state record per instant, the archive proper
#   restart/<YYYYmmddTHHMM>/   the complete restart sets, for restamping
#
# **Promoted from `bkg/`, not from the restart sets under `run/`.** The run
# records every state it produces as it produces it: `post.state` reduces the
# forecast's restart *and* every sub-window state beside it to one compressed
# record, filed under `bkg/` at the instant it is valid at. So the trajectory is
# already complete, already keyed by valid time, and already twenty times
# smaller than the restarts it came from, and `cleanup` is free to reap those on
# its ordinary schedule while the run is still going. This used to walk
# `run/<T>/rst/` and `run/<T>/slot/`, which meant an experiment being promoted
# had to pin every cycle it ever ran until this had been run by hand: 46 GB held
# to produce an archive of 1.8 GB.
#
# A record is not a restart. It holds the fields a comparison reads, quantized
# to five significant digits and masked to the ocean, with the coordinates a
# restart does not carry. That is everything `--truth-run` and `verify` want and
# it is not a state a model can be started from, so the complete restart sets
# the experiment pinned with `cleanup.keep_every` are promoted too, under
# `restart/`, which is what `tools/restamp-ic.sh` names to build an OSSE
# control. They are a handful against the trajectory's hundreds.
#
# Copies rather than symlinks or a move. The point of promotion is that the
# archive outlives the experiment: `cleanup` reaps `run/` on a schedule, and an
# archive of links into a reaped directory is an archive of dangling links. Once
# this has run the truth experiment's own output can be deleted.
#
# Idempotent by date: a state already in the archive is left alone and reported,
# so a run that was extended can be promoted again without re-copying what is
# already there.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

EXPERIMENT=${1:?usage: promote-truth.sh <experiment> <archive-name>}
ARCHIVE=${2:?usage: promote-truth.sh <experiment> <archive-name>}

: "${ACKBAR_STATIC_ROOT:?the site did not set ACKBAR_STATIC_ROOT}"
: "${ACKBAR_OUTPUT_ROOT:?the site did not set ACKBAR_OUTPUT_ROOT}"

EXP_DIR=$ACKBAR_OUTPUT_ROOT/$EXPERIMENT
CFG=$EXP_DIR/cfg/experiment.yaml
[[ -f $CFG ]] || { echo "promote-truth: no such experiment: $CFG" >&2; exit 1; }

# The domain and the cycle length come out of the frozen config rather than the
# command line, because both are properties of the experiment being promoted and
# a second spelling of the cycle length is a second chance to shift every date.
read -r DOMAIN LENGTH SOLVER < <("$ACKBAR_ROOT/.venv/bin/python" - "$CFG" <<'EOF'
import sys, yaml
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
config = yaml.safe_load(open(sys.argv[1]))
print(config["domain"]["name"], config["cycle"]["length"],
      config["solver"]["name"])
EOF
)

# A truth run is a free run. An experiment with an analysis has states that are
# not a trajectory of the model alone, and promoting one as truth would score
# every experiment against something that had already seen the observations.
if [[ $SOLVER != none ]]; then
    echo "promote-truth: $EXPERIMENT has solver '$SOLVER', not 'none'." >&2
    echo "promote-truth: a truth run must be a free run; its states are what" >&2
    echo "promote-truth: every experiment is scored against." >&2
    exit 1
fi

OUT=$ACKBAR_STATIC_ROOT/truth/$DOMAIN/$ARCHIVE
mkdir -p "$OUT"

copied=0
skipped=0

# The trajectory. One record per instant, already named for when it is valid,
# so this is a rename and never an arithmetic: `bkg/<T>/mem000.nc` becomes
# `<T>.nc`. The off-by-one-cycle error the old version had to reason about
# cannot arise here, because nothing recomputes a valid time.
for state_dir in "$EXP_DIR"/bkg/*/; do
    [[ -d $state_dir ]] || continue
    when=$(basename "$state_dir")
    [[ $when =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || continue
    record=$state_dir/mem000.nc
    [[ -f $record ]] || continue

    stamp=$(date -u -d "${when:0:8} ${when:9:2}:${when:11:2}" +%Y%m%dT%H%M)
    if [[ -f $OUT/$stamp.nc ]]; then
        skipped=$((skipped + 1))
        continue
    fi
    # Into a temporary name and then renamed, so that an interrupted copy never
    # leaves a half-written state behind under a date that looks promoted.
    cp -L "$record" "$OUT/.$stamp.nc.partial"
    mv "$OUT/.$stamp.nc.partial" "$OUT/$stamp.nc"
    copied=$((copied + 1))
done

# The restart sets the experiment pinned, for `tools/restamp-ic.sh`. Whatever
# `cleanup.keep_every` left behind: this does not ask for a cadence of its own,
# because the pin is the experiment's statement about which of its states are
# worth keeping whole and a second one here could only disagree.
pinned=0
for cycle_dir in "$EXP_DIR"/run/*/; do
    date=$(basename "$cycle_dir")
    [[ $date =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || continue
    [[ -f $cycle_dir/rst/mem000/MOM.res.nc ]] || continue

    # Valid one cycle after the directory that holds it. `run/<T>/rst/` is what
    # cycle T's *forecast* wrote, and stamping it T would put every pinned state
    # a cycle earlier than it is, which nothing downstream could detect.
    valid=$("$ACKBAR_ROOT/.venv/bin/python" - "$date" "$LENGTH" <<'EOF'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ackbar.duration import parse_duration, parse_instant
when = parse_instant(sys.argv[1]) + parse_duration(sys.argv[2])
print(when.strftime("%Y%m%dT%H%M"))
EOF
)
    [[ -d $OUT/restart/$valid ]] && { skipped=$((skipped + 1)); continue; }
    # `-L` is not optional. A cycle's restart directory is usually real files,
    # but cycle 0 is the *materialized initial condition*, which `ackbar create`
    # writes as symlinks into whatever offline product the experiment named. A
    # plain `cp -r` preserves those, and the archive then holds links into a
    # directory `cleanup` is entitled to delete: dangling links at best, and at
    # worst a later write through one of them reaching back and modifying the
    # original. Which is exactly what happened the first time this ran.
    mkdir -p "$OUT/restart"
    rm -rf "$OUT/restart/.$valid.partial"
    cp -rL "$cycle_dir/rst/mem000" "$OUT/restart/.$valid.partial"
    mv "$OUT/restart/.$valid.partial" "$OUT/restart/$valid"
    pinned=$((pinned + 1))
done

cat > "$OUT/README" <<EOF
Truth archive: $ARCHIVE, on $DOMAIN, promoted from the free run '$EXPERIMENT'.

One file per state, <YYYYmmddTHHMM>.nc, named for the instant it is valid at.
Read-only: it is what every experiment on this period is scored against, so a
state that changes after an experiment has been verified against it invalidates
that verification silently.

The cycle length of the run that produced it was $LENGTH, and the sub-window
states it wrote are here at their own cadence, indistinguishable from the rest
because there is nothing to distinguish: each is one instant.

**A state record is not a restart.** It holds temperature, salinity, layer
thickness, sea surface height and both velocity components, quantized to five
significant digits, masked to the ocean, with the coordinates a MOM6 restart
does not carry. That is what a comparison reads and it is not something a model
can be started from.

So restart/<YYYYmmddTHHMM>/ holds the complete restart sets the run pinned with
cleanup.keep_every, at that cadence and no other. Those are what
tools/restamp-ic.sh takes to build an OSSE control initial condition.

Regenerate with:
    tools/promote-truth.sh $EXPERIMENT $ARCHIVE
EOF

echo "promote-truth: $copied state(s) copied, $pinned restart set(s), $skipped already present"
echo "promote-truth: $OUT"
ls "$OUT" | head -5
