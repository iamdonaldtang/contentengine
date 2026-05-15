#!/usr/bin/env bash
# TaskOn Marketing Engine · systemd timer setup for VPS deployment.
#
# Writes 8 timer + service unit pairs to /etc/systemd/system, reloads
# systemd, enables and starts all timers. Idempotent.
#
# Usage:
#     sudo bash scripts/setup_systemd.sh                # install
#     sudo bash scripts/setup_systemd.sh --remove       # uninstall
#
# Required env (or detected from script location):
#     ENGINE_ROOT  — absolute path to engine root (default: parent of this script)
#     ENGINE_USER  — user account to run jobs as (default: taskon)
#     PYTHON_EXE   — python interpreter (default: $ENGINE_ROOT/.venv/bin/python)

set -euo pipefail

# ---- Resolve paths ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_ROOT="${ENGINE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENGINE_USER="${ENGINE_USER:-taskon}"
PYTHON_EXE="${PYTHON_EXE:-$ENGINE_ROOT/.venv/bin/python}"
SYSTEMD_DIR="/etc/systemd/system"

REMOVE=0
[[ "${1:-}" == "--remove" ]] && REMOVE=1

# ---- Sanity ----
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must run as root (sudo)" >&2
    exit 1
fi

if [[ $REMOVE -eq 0 ]]; then
    if [[ ! -x "$PYTHON_EXE" ]]; then
        echo "ERROR: python interpreter not found: $PYTHON_EXE" >&2
        echo "       create venv first: python3.12 -m venv $ENGINE_ROOT/.venv" >&2
        exit 1
    fi
    if ! id "$ENGINE_USER" &>/dev/null; then
        echo "ERROR: user '$ENGINE_USER' does not exist; create with:" >&2
        echo "       sudo useradd -r -s /bin/false -d $ENGINE_ROOT $ENGINE_USER" >&2
        exit 1
    fi
fi

echo "ENGINE_ROOT = $ENGINE_ROOT"
echo "ENGINE_USER = $ENGINE_USER"
echo "PYTHON_EXE  = $PYTHON_EXE"
echo

# ---- Task definitions: name | module | OnCalendar expression ----
# OnCalendar reference: https://www.freedesktop.org/software/systemd/man/systemd.time.html
declare -a TASKS=(
    "taskon-metrics-collector  | jobs.metrics_collector  | *-*-* 20:00:00"
    "taskon-attribution-engine | jobs.attribution_engine | *-*-* 21:00:00"
    "taskon-backup-sqlite      | scripts.backup_sqlite   | *-*-* 23:00:00"
    "taskon-kol-watch          | jobs.kol_watch          | Sun *-*-* 10:00:00"
    "taskon-weekly-reporter    | jobs.weekly_reporter    | Sun *-*-* 18:00:00"
    "taskon-performance-analyzer | jobs.performance_analyzer | Sun *-*-* 18:30:00"
    "taskon-monthly-reporter   | jobs.monthly_reporter   | Sun *-*-* 19:00:00"
    "taskon-topic-ranker       | jobs.topic_ranker       | Sun *-*-* 20:00:00"
    "taskon-update-btouch      | jobs.update_btouch      | Mon *-*-* 09:00:00"
    # Monthly jobs (1st Sunday of month). systemd's OnCalendar supports a
    # day-of-month range; "Sun *-*-01..07" restricts to the first Sunday
    # without requiring a script-side gate. The scripts still self-gate
    # defensively via ``is_first_sunday_of_month()``.
    "taskon-cohort-analysis     | jobs.cohort_analysis     | Sun *-*-01..07 19:30:00"
    "taskon-ab-aggregator       | jobs.ab_aggregator       | Sun *-*-01..07 20:00:00"
    "taskon-channel-attribution | jobs.channel_attribution | Sun *-*-01..07 20:30:00"
)

# ---- Remove mode ----
if [[ $REMOVE -eq 1 ]]; then
    echo "Removing all taskon-* timers + services..."
    for entry in "${TASKS[@]}"; do
        name="$(echo "$entry" | cut -d'|' -f1 | xargs)"
        systemctl stop  "${name}.timer"   2>/dev/null || true
        systemctl disable "${name}.timer" 2>/dev/null || true
        rm -f "${SYSTEMD_DIR}/${name}.timer"
        rm -f "${SYSTEMD_DIR}/${name}.service"
        echo "  removed $name"
    done
    systemctl daemon-reload
    echo "Done."
    exit 0
fi

# ---- Install ----
for entry in "${TASKS[@]}"; do
    name="$(echo   "$entry" | cut -d'|' -f1 | xargs)"
    module="$(echo "$entry" | cut -d'|' -f2 | xargs)"
    calend="$(echo "$entry" | cut -d'|' -f3 | xargs)"

    echo "[+] $name  ($calend)"

    # service unit
    cat > "${SYSTEMD_DIR}/${name}.service" <<EOF
[Unit]
Description=TaskOn Marketing Engine · ${module}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${ENGINE_USER}
WorkingDirectory=${ENGINE_ROOT}
EnvironmentFile=${ENGINE_ROOT}/.env
ExecStart=${PYTHON_EXE} -m ${module}
TimeoutStartSec=3600
Nice=10
StandardOutput=journal
StandardError=journal
# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=${ENGINE_ROOT}/runtime /var/lib/taskon /var/log/taskon
EOF

    # timer unit
    cat > "${SYSTEMD_DIR}/${name}.timer" <<EOF
[Unit]
Description=Timer for ${name}.service

[Timer]
OnCalendar=${calend}
Persistent=true
RandomizedDelaySec=60
Unit=${name}.service

[Install]
WantedBy=timers.target
EOF
done

echo
echo "Reloading systemd..."
systemctl daemon-reload

echo "Enabling + starting timers..."
for entry in "${TASKS[@]}"; do
    name="$(echo "$entry" | cut -d'|' -f1 | xargs)"
    systemctl enable --now "${name}.timer"
done

echo
echo "✅ Registered ${#TASKS[@]} timers."
echo
echo "Verify:"
echo "    systemctl list-timers 'taskon-*'"
echo
echo "Run a job manually:"
echo "    sudo systemctl start taskon-metrics-collector.service"
echo
echo "Logs:"
echo "    journalctl -u taskon-metrics-collector.service -f"
echo
echo "Uninstall:"
echo "    sudo bash scripts/setup_systemd.sh --remove"
