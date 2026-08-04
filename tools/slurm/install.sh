#!/usr/bin/env bash
# Install and configure a single-node Slurm on rancor, with slurmdbd accounting.
#
#   sudo ./install.sh              # run every stage
#   sudo ./install.sh config svc   # rerun selected stages
#
# Stages: pkgs munge dirs db config svc verify
# Every stage is idempotent; rerunning is safe.
#
# Docs: ../../docs/slurm.md

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DBNAME=slurm_acct_db
DBUSER=slurm
PWFILE=/etc/slurm/.dbpass          # root-only; lets stages rerun without a new password

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }

say() { printf '\n=== %s\n' "$*"; }

stage_pkgs() {
  say "packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  local want=(slurm-wlm slurmdbd munge)
  # Use whichever MySQL-compatible server is already present; otherwise install
  # mysql-server 8.0 to match the mysql-client already on this box and the
  # existing /var/lib/mysql datadir.
  if ! dpkg -l | grep -qE '^ii +(mysql-server|mariadb-server)'; then
    want+=(mysql-server)
  fi
  apt-get install -y "${want[@]}"
  systemctl enable --now mysql 2>/dev/null || systemctl enable --now mariadb
}

stage_munge() {
  say "munge"
  if [[ ! -s /etc/munge/munge.key ]]; then
    if command -v mungekey >/dev/null; then
      mungekey --create --keyfile /etc/munge/munge.key
    else
      /usr/sbin/create-munge-key -f
    fi
  fi
  chown munge:munge /etc/munge/munge.key
  chmod 400 /etc/munge/munge.key
  systemctl enable --now munge
  systemctl restart munge
  munge -n | unmunge >/dev/null && echo "munge round-trip ok"
}

stage_dirs() {
  say "directories"
  install -d -o slurm -g slurm -m 755 /var/spool/slurmctld /var/log/slurm
  install -d -o root  -g root  -m 755 /var/spool/slurmd
}

stage_db() {
  say "database"
  if [[ ! -s $PWFILE ]]; then
    install -m 600 -o root -g root /dev/null "$PWFILE"
    # Every stage in this pipeline drains its input; do not put `head` last or
    # pipefail turns the resulting SIGPIPE into a script-killing exit 141.
    head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-32 > "$PWFILE"
  fi
  local pw; pw=$(cat "$PWFILE")

  # slurmdbd's table layout wants a bigger innodb buffer and a long lock wait.
  cat > /etc/mysql/conf.d/slurmdbd.cnf <<'EOF'
[mysqld]
innodb_buffer_pool_size=1024M
innodb_log_file_size=64M
innodb_lock_wait_timeout=900
EOF
  systemctl restart mysql 2>/dev/null || systemctl restart mariadb

  mysql <<SQL
CREATE DATABASE IF NOT EXISTS ${DBNAME};
CREATE USER IF NOT EXISTS '${DBUSER}'@'localhost' IDENTIFIED BY '${pw}';
ALTER USER '${DBUSER}'@'localhost' IDENTIFIED BY '${pw}';
GRANT ALL PRIVILEGES ON ${DBNAME}.* TO '${DBUSER}'@'localhost';
FLUSH PRIVILEGES;
SQL
  echo "database ${DBNAME} and user ${DBUSER} ready"
}

stage_config() {
  say "config files"
  install -d -m 755 /etc/slurm
  install -o root -g root -m 644 "$HERE/slurm.conf"  /etc/slurm/slurm.conf
  install -o root -g root -m 644 "$HERE/cgroup.conf" /etc/slurm/cgroup.conf

  local pw; pw=$(cat "$PWFILE")
  install -o slurm -g slurm -m 600 /dev/null /etc/slurm/slurmdbd.conf
  sed "s|@DBPASS@|${pw}|" "$HERE/slurmdbd.conf.in" > /etc/slurm/slurmdbd.conf
  chown slurm:slurm /etc/slurm/slurmdbd.conf
  chmod 600 /etc/slurm/slurmdbd.conf
}

stage_svc() {
  say "services"
  # slurmdbd first: it creates the accounting tables that slurmctld registers into.
  systemctl enable --now slurmdbd
  for i in $(seq 30); do
    sacctmgr -n show cluster >/dev/null 2>&1 && break
    sleep 1
  done
  systemctl restart slurmdbd
  systemctl enable --now slurmctld
  systemctl enable --now slurmd
  systemctl restart slurmctld slurmd
}

stage_verify() {
  say "verify"
  systemctl --no-pager --lines=0 status munge slurmdbd slurmctld slurmd \
    | grep -E 'slurm|munge|Active:' || true
  echo
  sinfo -N -l || true
  echo
  scontrol show node rancor | head -12 || true
  echo
  echo "If the node shows DOWN/DRAIN:  scontrol update nodename=rancor state=resume"
  echo "Next, as your normal user:     tools/slurm/accounting-setup.sh"
}

stages=("$@")
[[ ${#stages[@]} -gt 0 ]] || stages=(pkgs munge dirs db config svc verify)
for s in "${stages[@]}"; do "stage_$s"; done
