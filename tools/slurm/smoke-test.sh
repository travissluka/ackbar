#!/usr/bin/env bash
# Exercise the bits of Slurm an HPC workflow engine actually depends on:
# submission, core/memory allocation, job arrays, afterok dependencies,
# cancellation, and post-mortem state from sacct.
#
#   tools/slurm/smoke-test.sh
#
# Runs as your normal user. Scratch lands in /data/mmw/test/slurm-smoke.

set -euo pipefail

WORK=/data/mmw/test/slurm-smoke
rm -rf "$WORK"; mkdir -p "$WORK"; cd "$WORK"

hr() { printf '\n--- %s\n' "$*"; }

hr "cluster"
sinfo -l
scontrol show partition | grep -E 'PartitionName|MaxTime|Default='

hr "1. hello, 1 core, compute partition (default)"
cat > hello.sh <<'EOF'
#!/bin/bash
#SBATCH --job-name=hello
#SBATCH --output=hello-%j.out
#SBATCH --ntasks=1
#SBATCH --time=00:02:00
echo "host=$(hostname) job=$SLURM_JOB_ID cpus=$SLURM_CPUS_ON_NODE part=$SLURM_JOB_PARTITION"
sleep 5
EOF
j1=$(sbatch --parsable hello.sh)
echo "submitted $j1"

hr "2. 8 cores + memory request, debug partition"
cat > wide.sh <<'EOF'
#!/bin/bash
#SBATCH --job-name=wide
#SBATCH --output=wide-%j.out
#SBATCH --partition=debug
#SBATCH --nodes=1
#SBATCH --ntasks=8
#SBATCH --cpus-per-task=1
#SBATCH --mem=8G
#SBATCH --time=00:05:00
echo "tasks=$SLURM_NTASKS cpus_on_node=$SLURM_CPUS_ON_NODE mem=$SLURM_MEM_PER_NODE"
srun --cpu-bind=cores hostname
EOF
j2=$(sbatch --parsable wide.sh)
echo "submitted $j2"

hr "3. job array of 4"
cat > arr.sh <<'EOF'
#!/bin/bash
#SBATCH --job-name=arr
#SBATCH --output=arr-%A_%a.out
#SBATCH --array=1-4
#SBATCH --ntasks=1
#SBATCH --time=00:02:00
echo "array task $SLURM_ARRAY_TASK_ID of $SLURM_ARRAY_JOB_ID"
sleep 3
EOF
j3=$(sbatch --parsable arr.sh)
echo "submitted $j3"

hr "4. afterok dependency chain (the cycling pattern)"
cat > dep.sh <<'EOF'
#!/bin/bash
#SBATCH --job-name=dep
#SBATCH --output=dep-%j.out
#SBATCH --ntasks=1
#SBATCH --time=00:02:00
echo "ran after its dependency at $(date -u +%FT%TZ)"
EOF
j4=$(sbatch --parsable --dependency=afterok:"$j3" dep.sh)
echo "submitted $j4 dependent on $j3"

hr "5. deliberate failure, to check nonzero exit is reported"
cat > fail.sh <<'EOF'
#!/bin/bash
#SBATCH --job-name=fail
#SBATCH --output=fail-%j.out
#SBATCH --ntasks=1
#SBATCH --time=00:02:00
echo "about to fail"
exit 42
EOF
j5=$(sbatch --parsable fail.sh)
echo "submitted $j5"

hr "6. submit then cancel, to check CANCELLED state"
j6=$(sbatch --parsable --begin=now+1hour hello.sh)
scancel "$j6"
echo "submitted and cancelled $j6"

hr "queue"
squeue -l

hr "waiting for the chain to drain"
while squeue -h -u "$USER" | grep -q .; do sleep 3; done
echo "queue empty"

hr "sacct (this is what the workflow engine will poll)"
sacct -j "$j1,$j2,$j3,$j4,$j5,$j6" \
  --format=JobID%16,JobName%10,Partition,AllocCPUS,State%20,ExitCode,Elapsed

hr "job output"
for f in hello-*.out wide-*.out arr-*.out dep-*.out fail-*.out; do
  [[ -e $f ]] || continue
  printf '%s: %s\n' "$f" "$(head -c 200 "$f" | tr '\n' ' ')"
done

hr "expected"
cat <<EOF
  $j1 hello   COMPLETED  0:0
  $j2 wide    COMPLETED  0:0   AllocCPUS=8
  $j3 arr     COMPLETED  0:0   (4 array tasks)
  $j4 dep     COMPLETED  0:0   (ran only after $j3)
  $j5 fail    FAILED     42:0
  $j6 hello   CANCELLED  0:0
Scratch left in $WORK
EOF
