#!/bin/bash
# One free forecast from a restart set, with parameters perturbed, writing daily
# snapshots. The unit of work behind `tools/spike-sweep.py`.
#
#   tools/spike-forecast.sh <domain> <ic-dir> <days> <out-dir> [KEY=VALUE ...]
#
#   tools/spike-forecast.sh gom_25km \
#       $ACKBAR_STATIC_ROOT/ic/gom_25km/osse-control-25km/20150712T00 \
#       5 /data/ackbar/spike/kd-3e-5  KD=3.0E-05
#
# The `KEY=VALUE` arguments become a third MOM6 parameter file, `MOM_sweep`,
# read after `MOM_input` and `MOM_override`. A key already set by either of
# those gets `#override` in front of it, which MOM6's parser requires and whose
# absence is a fatal rather than a silently ignored line; a key set by neither
# is written plain, because `#override` on an unset parameter is also an error.
# Deciding that here rather than at the call site means a caller names a
# parameter and a value and never has to know which file the domain set it in.
#
# The domain's own MOM_override is left untouched. A sweep that edited it would
# be editing the thing every other experiment on this domain reads.
#
# Perturbing *nothing* is a legal and necessary call: it produces the control
# the spread is measured against, through exactly the same code path, so a
# difference between control and member cannot be an artifact of how each was
# staged.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ACKBAR_ROOT/site/activate.sh"

DOMAIN=${1:?usage: spike-forecast.sh <domain> <ic-dir> <days> <out-dir> [KEY=VALUE ...]}
IC=${2:?usage: spike-forecast.sh <domain> <ic-dir> <days> <out-dir> [KEY=VALUE ...]}
DAYS=${3:?usage: spike-forecast.sh <domain> <ic-dir> <days> <out-dir> [KEY=VALUE ...]}
OUT=${4:?usage: spike-forecast.sh <domain> <ic-dir> <days> <out-dir> [KEY=VALUE ...]}
shift 4

: "${ACKBAR_STATIC_ROOT:?the site did not set ACKBAR_STATIC_ROOT}"
: "${ACKBAR_SCRATCH_ROOT:?the site did not set ACKBAR_SCRATCH_ROOT}"

# Ranks per run, and how many of these run at once, are two halves of one
# decision that only the caller can make, so neither is fixed here. The site's
# value is the default because a single run should use the machine.
NTASKS=${NTASKS:-${ACKBAR_MPI_TASKS:?the site did not set ACKBAR_MPI_TASKS}}

# The worktree a sweep is developed in has no populated `pkg/`, so the
# executable is overridable. The default is the checkout's own build, which is
# the only thing an experiment is allowed to run.
EXE=${SPIKE_EXE:-$ACKBAR_ROOT/pkg/mom6sis2/ice_ocean_SIS2/build/coupler_main}

source "$ACKBAR_ROOT/tools/domain-paths.sh"
domain_paths "$DOMAIN"

for path in "$EXE" "$BASE/MOM_input" "$OVERRIDE/MOM_override" \
            "$OVERRIDE/SIS_override" "$DATA" "$IC/coupler.res" "$IC/MOM.res.nc"; do
    [[ -e $path ]] || { echo "spike-forecast: $path does not exist" >&2; exit 1; }
done

# The date the model resumes from, read out of the restart set rather than
# passed in. The whole point of a sweep is that every member starts from one
# state, so a start date that could disagree with the state is a way for that
# to quietly stop being true.
read -r YY MM DD HH <<< "$(awk 'NR==3 {print $1, $2, $3, $4}' "$IC/coupler.res")"
[[ -n ${HH:-} ]] || { echo "spike-forecast: cannot read a current time from $IC/coupler.res" >&2; exit 1; }

WORK=$ACKBAR_SCRATCH_ROOT/spike/$(basename "$OUT").$$
rm -rf "$WORK"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

mkdir INPUT RESTART
for path in "$DATA"/*; do ln -s "$(readlink -f "$path")" "INPUT/$(basename "$path")"; done
# The restart set over the top of the domain data, which is the order
# `ackbar.mom6sis2._input_dir` uses and for the same reason: `coupler_main`
# hardcodes `INPUT/coupler.res`, so a restart set has to land in `INPUT`.
for path in "$IC"/*; do
    [[ -f $path ]] || continue
    ln -sf "$(readlink -f "$path")" "INPUT/$(basename "$path")"
done

for name in data_table field_table MOM_input SIS_input; do cp "$BASE/$name" .; done

# A different atmosphere is a different data table plus the file it names, so
# the two arrive together or not at all. `SPIKE_LINK` is `name=path` pairs,
# linked into `INPUT` after the restart set so a member's own forcing wins over
# anything the domain staged under the same name.
# Both or neither, which is the rule `mom6sis2.stage` enforces for the workflow.
# A spike is where a forcing source is tried first, so a spike that can run the
# one combination that completes, reports success and doubles the diurnal cycle
# is the worst place to leave the hole.
if { [[ -n ${SPIKE_DATA_TABLE:-} ]] && [[ -z ${SPIKE_SIS:-} ]]; } ||
   { [[ -z ${SPIKE_DATA_TABLE:-} ]] && [[ -n ${SPIKE_SIS:-} ]]; }; then
    echo "spike-forecast: SPIKE_DATA_TABLE and SPIKE_SIS are set without each" \
         "other. A forcing source is both: the table says which file the fluxes" \
         "come from, and SIS_forcing says what that file's cadence implies" \
         "about the model. A sub-daily shortwave read with ADD_DIURNAL_SW left" \
         "on runs to completion and applies the diurnal cycle twice." >&2
    exit 1
fi

if [[ -n ${SPIKE_DATA_TABLE:-} ]]; then
    [[ -e $SPIKE_DATA_TABLE ]] || {
        echo "spike-forecast: $SPIKE_DATA_TABLE does not exist" >&2; exit 1; }
    cp "$SPIKE_DATA_TABLE" data_table
fi
for pair in ${SPIKE_LINK:-}; do
    name=${pair%%=*}
    path=${pair#*=}
    [[ -e $path ]] || { echo "spike-forecast: $path does not exist" >&2; exit 1; }
    ln -sfn "$(readlink -f "$path")" "INPUT/$name"
done
cp "$OVERRIDE/MOM_override" "$OVERRIDE/SIS_override" .

# `SPIKE_SIS` is a fourth SIS parameter file, read after SIS_override, and it
# travels with the data table for the same reason the data table travels with
# the file it names: `ADD_DIURNAL_SW` synthesizes a diurnal cycle on the
# assumption that the shortwave it is handed is a daily mean, so it is a property
# of how often the *forcing* reports and not of the domain. An hourly atmosphere
# read with it on carries the diurnal cycle twice.
#
# Appended rather than replacing SIS_override, because the domain's SIS_override
# is where NIGLOBAL and NJGLOBAL live and a run without them is a run on the
# wrong grid. Always written, empty when unset, so the namelist below can name it
# unconditionally.
: > SIS_forcing
echo "! written by tools/spike-forecast.sh" >> SIS_forcing
if [[ -n ${SPIKE_SIS:-} ]]; then
    [[ -e $SPIKE_SIS ]] || {
        echo "spike-forecast: $SPIKE_SIS does not exist" >&2; exit 1; }
    cat "$SPIKE_SIS" >> SIS_forcing
fi
ln -s "$EXE" coupler_main

# The perturbation, as its own parameter file so that what a member changed is
# one `cat` away for as long as the output survives.
: > MOM_sweep
echo "! written by tools/spike-forecast.sh" >> MOM_sweep
for pair in "$@"; do
    key=${pair%%=*}
    value=${pair#*=}
    [[ $key != "$pair" ]] || { echo "spike-forecast: '$pair' is not KEY=VALUE" >&2; exit 1; }
    if grep -qE "^[[:space:]]*(#override[[:space:]]+)?$key[[:space:]]*=" MOM_input MOM_override; then
        echo "#override $key = $value" >> MOM_sweep
    else
        echo "$key = $value" >> MOM_sweep
    fi
done

# Daily snapshots, not means. Spread is a property of the states an ensemble
# holds at an instant, and a daily mean of a growing perturbation understates it
# by averaging over its own growth.
cat > diag_table <<EOF
spike
$YY $MM $DD $HH 0 0
"ocn_daily", 1, "days", 1, "days", "time"
"ocean_model", "SSH",     "SSH",     "ocn_daily", "all", "none", "none", 2
"ocean_model", "SST",     "SST",     "ocn_daily", "all", "none", "none", 2
"ocean_model", "SSS",     "SSS",     "ocn_daily", "all", "none", "none", 2
"ocean_model", "MLD_003", "MLD_003", "ocn_daily", "all", "none", "none", 2
"ocean_model", "temp",    "temp",    "ocn_daily", "all", "none", "none", 2
"ocean_model", "salt",    "salt",    "ocn_daily", "all", "none", "none", 2
"ocean_model", "u",       "u",       "ocn_daily", "all", "none", "none", 2
"ocean_model", "v",       "v",       "ocn_daily", "all", "none", "none", 2
EOF

# Extra diagnostics, one `"ocean_model", "<name>", ...` line per field. A
# parameter that resolves and then changes nothing is either being ignored or
# multiplying zero, and the only way to tell is to look at the field it acts
# through.
for name in ${SPIKE_DIAG:-}; do
    printf '"ocean_model", "%s", "%s", "ocn_daily", "all", "none", "none", 2\n' \
        "$name" "$name" >> diag_table
done

python3 - "$BASE/input.nml" "$DAYS" "$YY,$MM,$DD,$HH,0,0" <<'PYEOF' > input.nml
import re, sys
text, days, start = open(sys.argv[1]).read(), sys.argv[2], sys.argv[3]

def group(name, edits):
    global text
    def patch(match):
        body = match.group(0)
        for key, value in edits.items():
            pattern = rf"^(\s*){key}\s*=[^\n]*(?:\n(?!\s*(?:\w+\s*=|/|$))[^\n]*)*"
            if re.search(pattern, body, re.M):
                body = re.sub(pattern, rf"\g<1>{key} = {value},", body, flags=re.M)
            else:
                body = body.rstrip()[:-1].rstrip() + f"\n  {key} = {value},\n/"
        return body
    text = re.sub(rf"&{name}\b.*?^\s*/\s*$", patch, text, flags=re.S | re.M)

group("coupler_nml", {"months": 0, "days": days, "hours": 0,
                      "current_date": start, "force_date_from_namelist": ".false."})
# `'r'` is what makes this a resumed run rather than a cold start off ic.nc.
# Without it the model reads the z-level initial condition, ignores the restart
# set it was handed, and every member starts from a state months away from the
# one the sweep is about.
group("MOM_input_nml", {"parameter_filename": "'MOM_input', 'MOM_override', 'MOM_sweep'",
                        "input_filename": "'r'"})
group("SIS_input_nml",
      {"parameter_filename": "'SIS_input', 'SIS_override', 'SIS_forcing'",
       "input_filename": "'r'"})
sys.stdout.write(text)
PYEOF

# The NOAA-PSL pattern generator reads its own `&nam_stochy` group out of
# `input.nml`, which nothing in the MOM6 parameter files can carry. Appended
# rather than patched in: the group does not exist in any base case, and MOM6's
# own namelist reader ignores groups it was not asked for.
#
# Both halves have to agree. `DO_SPPT` in MOM_sweep turns the *application* on
# inside MOM6, `ocnsppt` in this group turns the *pattern* on inside the
# generator, and `init_stochastic_physics_ocn` refuses the run when they
# disagree rather than silently applying a pattern of zeros.
if [[ -n ${SPIKE_NAMELIST:-} ]]; then
    [[ -e $SPIKE_NAMELIST ]] || {
        echo "spike-forecast: $SPIKE_NAMELIST does not exist" >&2; exit 1; }
    cat "$SPIKE_NAMELIST" >> input.nml
    cp "$SPIKE_NAMELIST" nam_stochy.used
fi

start=$SECONDS
mpiexec -n "$NTASKS" ./coupler_main > model.log 2>&1 || {
    # The log outlives the scratch directory, because the run that failed is
    # the one whose log is worth having and the trap is about to delete it.
    # `tail` alone is not enough: MOM6 prints its FATAL before the backtrace,
    # and a backtrace is long enough to push the sentence that explains it out
    # of any window worth printing to a terminal.
    mkdir -p "$OUT"
    cp model.log "$OUT/model.log.failed" 2>/dev/null || true
    echo "spike-forecast: the model failed; whole log at $OUT/model.log.failed" >&2
    grep -iE 'FATAL|ERROR' model.log | head -10 >&2
    exit 1
}
elapsed=$((SECONDS - start))

# Artifacts, not the exit code. `mpiexec` has been known to return 0 over a
# model that wrote nothing.
shopt -s nullglob
written=(*ocn_daily.nc)
[[ ${#written[@]} -gt 0 ]] || {
    echo "spike-forecast: the model exited 0 and wrote no ocn_daily output" >&2
    tail -20 model.log >&2
    exit 1
}

mkdir -p "$OUT"
mv "${written[@]}" "$OUT/"
cp MOM_sweep model.log ocean.stats "$OUT/" 2>/dev/null || true
# What MOM6 resolved, as against what it was asked for. A parameter written
# into MOM_sweep and then ignored, because the module that reads it is gated on
# something else, is indistinguishable from a parameter that had no effect, and
# this file is the only thing that tells the two apart.
cp MOM_parameter_doc.short "$OUT/" 2>/dev/null || true
[[ -e nam_stochy.used ]] && cp nam_stochy.used "$OUT/"
printf '%s\n' "$elapsed" > "$OUT/seconds"
echo "spike-forecast: $OUT (${elapsed}s on $NTASKS PEs)"
