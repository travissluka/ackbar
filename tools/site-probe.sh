#!/bin/bash
# Print what a site file has to be told, on the machine it describes.
#
#   tools/site-probe.sh
#
# Every value a new site/<name>.sh needs and cannot be guessed from another
# machine: the queues this account may use, the launcher flags this Slurm
# accepts, the two queue caps, and how a failed dependency behaves. Read-only;
# it submits nothing and writes nothing.
#
# This exists because the alternative is finding out one value at a time, three
# cycles into a run, from a job that stopped without saying why. It is the first
# thing to run when porting ACKBAR, before the site file rather than after.
#
# Deliberately does NOT source site/activate.sh: it runs before there is a site
# file to source, and it is the answers here that make one writable.

set -uo pipefail

section() { printf '\n=== %s\n' "$1"; }
try() { command -v "${1%% *}" >/dev/null 2>&1 || { echo "  (no ${1%% *})"; return 1; }; }

section "host"
echo "  hostname -s : $(hostname -s)     <- the default ACKBAR_SITE"
echo "  user        : ${USER:-?}"

section "what this account may submit to  -> ACKBAR_PARTITION/_ACCOUNT/_QOS"
if try sacctmgr; then
    sacctmgr -nP show assoc user="$USER" format=account,partition,qos 2>/dev/null \
        | sed 's/^/  /' || echo "  (sacctmgr refused; ask the help desk)"
fi
if try sinfo; then
    echo "  partitions (name, default, time limit, cores/node):"
    sinfo -h -o '    %P  %l  %c' | sort -u
fi

section "QoS limits  -> ACKBAR_MAX_SUBMIT_JOBS"
# The per-user QoS cap is what actually rejects an sbatch on a shared machine;
# MaxJobCount below is the cluster-wide one and is rarely what you hit.
if try sacctmgr; then
    sacctmgr -nP show qos format=name,maxsubmitpu,maxjobspu 2>/dev/null \
        | sed 's/^/  /' | head -20
fi

section "cluster caps  -> ACKBAR_MAX_SUBMIT_JOBS, ACKBAR_MAX_ARRAY_SIZE"
if try scontrol; then
    scontrol show config 2>/dev/null \
        | grep -E 'MaxJobCount|MaxArraySize|MaxSubmitJobs|MinJobAge|MpiDefault' \
        | sed 's/^/  /'
fi

section "dependency handling  -> what a failure looks like in ackbar status"
if try scontrol; then
    scontrol show config 2>/dev/null | grep -i 'DependencyParameters' | sed 's/^/  /'
    echo "  kill_invalid_depend present: a job whose dependency can never be met is"
    echo "  killed. Absent: it pends forever. Both are handled; they look different."
fi

section "launcher  -> ACKBAR_LAUNCHER, ACKBAR_OFFLINE_LAUNCHER"
if try srun; then
    echo "  srun --mpi=list:"
    srun --mpi=list 2>&1 | sed 's/^/    /'
fi
echo "  A wrong --mpi does not fail. It starts N ranks each in its own"
echo "  MPI_COMM_WORLD and Slurm records COMPLETED. Verify the choice with a"
echo "  program that prints MPI_Comm_size, never with an exit code."

section "filesystems  -> the _work line in the site file"
echo "  Roots must be visible to the compute nodes at the same path, and hold"
echo "  an experiment's output (tens to hundreds of GB). Candidates:"
for d in /work /work2 /scratch /lustre /data "$HOME"; do
    [[ -d $d ]] && printf '    %-10s %s\n' "$d" \
        "$(df -h "$d" 2>/dev/null | awk 'NR==2{print $4" free of "$2}')"
done

section "toolchain"
echo "  module list:"
module list 2>&1 | sed 's/^/    /' | head -30

exit 0
