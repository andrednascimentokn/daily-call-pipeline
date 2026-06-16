#!/usr/bin/env bash
# Bootstrap script for the daily-calls pipeline on a fresh Contabo VPS (Ubuntu 24.04 LTS).
#
# Pre-requisites BEFORE running this script:
#   - You are root (or sudo -i).
#   - The repo has been cloned to /opt/daily-calls/  (e.g. via scp or git clone).
#   - /etc/daily-calls/.env exists with all required vars (see .env.example).
#   - /etc/daily-calls/gcs-sa.json exists with the GCS service account JSON key.
#
# What this script does:
#   1. Installs OS deps (python3.11, ffmpeg).
#   2. Creates a venv at /opt/daily-calls/venv and installs requirements.
#   3. Locks down /etc/daily-calls/ (root-only, chmod 600).
#   4. Installs a cron entry running at 04:00 Europe/Lisbon.
#   5. Creates the log directory at /var/log/daily-calls/.
#
# Re-runnable: each step is idempotent.

set -euo pipefail

REPO_DIR=${REPO_DIR:-/opt/daily-calls}
ETC_DIR=${ETC_DIR:-/etc/daily-calls}
LOG_DIR=${LOG_DIR:-/var/log/daily-calls}
VENV_DIR="$REPO_DIR/venv"
PYTHON_BIN=${PYTHON_BIN:-python3.12}  # Ubuntu 24.04 default; override with env var if needed

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (or via sudo)." >&2
  exit 1
fi

echo "==> apt update + install deps"
apt-get update -qq
apt-get install -y --no-install-recommends \
  "$PYTHON_BIN" \
  "${PYTHON_BIN}-venv" \
  ffmpeg \
  ca-certificates

echo "==> creating dirs"
mkdir -p "$REPO_DIR" "$ETC_DIR" "$LOG_DIR"
chmod 700 "$ETC_DIR"

if [[ ! -f "$REPO_DIR/requirements.txt" ]]; then
  echo "ERROR: $REPO_DIR/requirements.txt not found. Did you clone the repo to $REPO_DIR ?" >&2
  exit 1
fi

if [[ ! -f "$ETC_DIR/.env" ]]; then
  echo "ERROR: $ETC_DIR/.env not found. Copy from .env.example and fill in secrets first." >&2
  exit 1
fi
if [[ ! -f "$ETC_DIR/gcs-sa.json" ]]; then
  echo "ERROR: $ETC_DIR/gcs-sa.json not found. Place the GCS service account JSON there." >&2
  exit 1
fi
chmod 600 "$ETC_DIR/.env" "$ETC_DIR/gcs-sa.json"

echo "==> creating venv at $VENV_DIR"
if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt"

echo "==> installing cron entry (04:00 Europe/Lisbon)"
# IMPORTANTE: Ubuntu cron NÃO usa a directiva TZ= para interpretar schedules — apenas
# como env var para o processo. O schedule é sempre em SYSTEM time.
# Contabo VPS (Alemanha) corre em CEST/CET, que é sempre Lisbon+1h ano inteiro
# (Portugal e Continental Europe entram em DST no mesmo dia).
# Logo: "0 5" em system time = 04:00 Lisbon, year-round.
# Se deploys noutro fuso, ajusta a hora aqui ou faz `timedatectl set-timezone Europe/Lisbon`.
CRON_LINE="0 5 * * * $VENV_DIR/bin/python $REPO_DIR/run_pipeline.py >> $LOG_DIR/cron.log 2>&1"
# Weekly overview report — Sexta 07:00 Lisbon = 08:00 system time (system = Lisbon+1h).
WEEKLY_CRON_LINE="0 8 * * 5 $VENV_DIR/bin/python $REPO_DIR/run_weekly.py >> $LOG_DIR/cron.log 2>&1"
TZ_LINE="TZ=Europe/Lisbon"

# Build the new crontab idempotently
TMP_CRON=$(mktemp)
crontab -l 2>/dev/null | grep -v "$REPO_DIR/run_pipeline.py" | grep -v "$REPO_DIR/run_weekly.py" | grep -v "^TZ=Europe/Lisbon$" > "$TMP_CRON" || true
echo "$TZ_LINE" >> "$TMP_CRON"
echo "$CRON_LINE" >> "$TMP_CRON"
echo "$WEEKLY_CRON_LINE" >> "$TMP_CRON"
crontab "$TMP_CRON"
rm "$TMP_CRON"

echo "==> done. Cron is now scheduled:"
crontab -l | grep -E "run_pipeline.py|run_weekly.py|TZ="
echo
echo "Sanity check (dry-run, no email, no GCS write):"
echo "  sudo $VENV_DIR/bin/python $REPO_DIR/run_pipeline.py --date 2026-05-27 --no-email --no-gcs --dry-run"
