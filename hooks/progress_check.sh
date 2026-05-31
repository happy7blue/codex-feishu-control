#!/usr/bin/env bash
set -u

PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
HOOKS_DIR="$CODEX_HOME/hooks"
LOG_DIR="$CODEX_HOME/logs"
LOG_FILE="$LOG_DIR/progress_check.log"
DEVICE_NAME="${CODEX_PROGRESS_DEVICE_NAME:-Mini 2}"

PGREP_BIN="${PGREP_BIN:-/usr/bin/pgrep}"
PS_BIN="${PS_BIN:-/bin/ps}"
DATE_BIN="${DATE_BIN:-/bin/date}"
SED_BIN="${SED_BIN:-/usr/bin/sed}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"

log_line() {
  /bin/mkdir -p "$LOG_DIR"
  /usr/bin/printf '%s [%s] %s\n' "$("$DATE_BIN" '+%Y-%m-%d %H:%M:%S %Z')" "$1" "$2" >>"$LOG_FILE"
}

format_duration() {
  local total="$1"
  local days hours minutes seconds

  if [ -z "$total" ] || [ "$total" -lt 0 ]; then
    total=0
  fi

  days=$((total / 86400))
  hours=$(((total % 86400) / 3600))
  minutes=$(((total % 3600) / 60))
  seconds=$((total % 60))

  if [ "$days" -gt 0 ]; then
    /usr/bin/printf '%s天%s小时%s分' "$days" "$hours" "$minutes"
  elif [ "$hours" -gt 0 ]; then
    /usr/bin/printf '%s小时%s分%s秒' "$hours" "$minutes" "$seconds"
  elif [ "$minutes" -gt 0 ]; then
    /usr/bin/printf '%s分%s秒' "$minutes" "$seconds"
  else
    /usr/bin/printf '%s秒' "$seconds"
  fi
}

pids="$("$PGREP_BIN" -f "codex exec" 2>/dev/null || true)"
if [ -z "$pids" ]; then
  exit 0
fi

now_epoch="$("$DATE_BIN" '+%s')"
process_count=0
longest_elapsed=0
pid_list=""

for pid in $pids; do
  case "$pid" in
    ''|*[!0-9]*)
      continue
      ;;
  esac

  if ! "$PS_BIN" -p "$pid" >/dev/null 2>&1; then
    continue
  fi

  command_line="$("$PS_BIN" -p "$pid" -o command= 2>/dev/null || true)"
  case "$command_line" in
    *"pgrep -f codex exec"*|*"pgrep -fl codex exec"*)
      continue
      ;;
    *"codex exec"*)
      ;;
    *)
      continue
      ;;
  esac

  process_count=$((process_count + 1))
  if [ -z "$pid_list" ]; then
    pid_list="$pid"
  else
    pid_list="$pid_list,$pid"
  fi

  lstart="$("$PS_BIN" -p "$pid" -o lstart= 2>/dev/null | "$SED_BIN" 's/^ *//;s/ *$//')"
  if [ -z "$lstart" ]; then
    continue
  fi

  start_epoch="$(LC_ALL=C "$DATE_BIN" -j -f '%a %b %e %T %Y' "$lstart" '+%s' 2>/dev/null || true)"
  case "$start_epoch" in
    ''|*[!0-9]*)
      continue
      ;;
  esac

  elapsed=$((now_epoch - start_epoch))
  if [ "$elapsed" -gt "$longest_elapsed" ]; then
    longest_elapsed="$elapsed"
  fi
done

if [ "$process_count" -eq 0 ]; then
  exit 0
fi

duration_label="$(format_duration "$longest_elapsed")"
timestamp="$("$DATE_BIN" '+%Y-%m-%d %H:%M:%S')"
message="$(/usr/bin/printf '【Codex 进度检查】\n时间：%s\n设备：%s\n状态：有 Codex exec 进程在运行\n已运行时长：%s（最长进程）\n进程数：%s\n进程PID：%s' "$timestamp" "$DEVICE_NAME" "$duration_label" "$process_count" "$pid_list")"

if [ "${CODEX_PROGRESS_DRY_RUN:-0}" = "1" ]; then
  /usr/bin/printf '%s\n' "$message"
  exit 0
fi

if [ ! -f "$HOOKS_DIR/feishu.env" ]; then
  log_line "warning" "feishu.env not found; notification skipped"
  exit 1
fi

if [ ! -f "$HOOKS_DIR/notify_feishu.py" ]; then
  log_line "warning" "notify_feishu.py not found; notification skipped"
  exit 1
fi

if ! CODEX_PROGRESS_MESSAGE="$message" "$PYTHON_BIN" - >>"$LOG_FILE" 2>&1 <<'PY'
from __future__ import annotations

import importlib.util
import os
import sys
import traceback
from pathlib import Path


def fallback_log(text: str) -> None:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    log_file = codex_home / "logs" / "progress_check.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(text.rstrip() + "\n")


try:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    hooks_dir = codex_home / "hooks"
    notify_script = hooks_dir / "notify_feishu.py"
    env_file = hooks_dir / "feishu.env"
    message = os.environ.get("CODEX_PROGRESS_MESSAGE", "").strip()

    if not message:
        raise RuntimeError("progress message is empty")

    spec = importlib.util.spec_from_file_location("codex_notify_feishu", notify_script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load notifier spec: {notify_script}")

    notifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notifier)
    env_values = notifier.load_env_file(env_file)
    sent, method = notifier.send_notification(message, env_values)
    notifier.write_log(
        "info" if sent else "warning",
        "progress check notification processed",
        sent=bool(sent),
        method=str(method),
    )
    raise SystemExit(0 if sent else 1)
except SystemExit:
    raise
except Exception:
    fallback_log(traceback.format_exc())
    raise SystemExit(1)
PY
then
  log_line "warning" "progress notification failed"
  exit 1
fi
