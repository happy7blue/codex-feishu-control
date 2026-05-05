#!/usr/bin/env python3
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import platform
import socket
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import fcntl
from pathlib import Path


CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
HOOKS_DIR = CODEX_HOME / "hooks"
LOG_DIR = CODEX_HOME / "logs"
ENV_FILE = HOOKS_DIR / "feishu.env"
LOG_FILE = LOG_DIR / "notify_feishu.log"
STATE_FILE = LOG_DIR / "notify_feishu_state.json"
MAX_FIELD_LEN = 1600
DEFAULT_FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
DEFAULT_STOP_MIN_INTERVAL_SECONDS = 10
DEFAULT_ROOT_STOP_DUPLICATE_WINDOW_SECONDS = 10
DEFAULT_TOOL_FAILURE_MIN_INTERVAL_SECONDS = 300


def now_local() -> str:
    return _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def compact_line(text: str, limit: int = 120) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def extract_content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, dict):
                value = item.get("text") or item.get("input_text") or item.get("content")
                if isinstance(value, str):
                    chunks.append(value)
            elif isinstance(item, str):
                chunks.append(item)
        return "\n".join(chunks)
    return ""


def is_system_scaffold_message(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith(
        (
            "# AGENTS.md instructions",
            "<environment_context>",
            "<permissions instructions>",
            "<app-context>",
            "<collaboration_mode>",
            "<skills_instructions>",
            "<plugins_instructions>",
        )
    )


def last_user_message_from_transcript(path_value: str) -> str:
    if not path_value:
        return ""
    try:
        path = Path(path_value).expanduser().resolve()
        codex_home = CODEX_HOME.expanduser().resolve()
        if codex_home not in path.parents and path != codex_home:
            return ""
        if not path.exists() or not path.is_file():
            return ""
        max_bytes = 512 * 1024
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes), os.SEEK_SET)
            raw = fh.read().decode("utf-8", errors="replace")
        for line in reversed(raw.splitlines()):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = row.get("payload") if isinstance(row, dict) else None
            if not isinstance(payload, dict):
                continue
            if payload.get("type") != "message" or payload.get("role") != "user":
                continue
            text = extract_content_text(payload.get("content")).strip()
            if text and not is_system_scaffold_message(text):
                return text
    except Exception as exc:
        write_log("debug", "failed to read transcript task description", error=str(exc))
    return ""


def cwd_from_transcript(path_value: str) -> str:
    if not path_value:
        return ""
    try:
        path = Path(path_value).expanduser().resolve()
        codex_home = CODEX_HOME.expanduser().resolve()
        if codex_home not in path.parents and path != codex_home:
            return ""
        if not path.exists() or not path.is_file():
            return ""
        max_bytes = 512 * 1024
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes), os.SEEK_SET)
            raw = fh.read().decode("utf-8", errors="replace")
        for line in reversed(raw.splitlines()):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = row.get("payload") if isinstance(row, dict) else None
            if not isinstance(payload, dict):
                continue
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd.strip() and Path(cwd) != Path("/"):
                return cwd.strip()
    except Exception as exc:
        write_log("debug", "failed to read transcript cwd", error=str(exc))
    return ""


def latest_active_workspace_root() -> str:
    path = CODEX_HOME / ".codex-global-state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        roots = state.get("active-workspace-roots") or state.get("electron-saved-workspace-roots") or []
        if not isinstance(roots, list):
            return ""
        for root in roots:
            if isinstance(root, str) and root.strip() and Path(root).expanduser().exists():
                return root.strip()
    except Exception as exc:
        write_log("debug", "failed to read active workspace root", error=str(exc))
    return ""


def display_cwd(data: dict) -> str:
    raw = data.get("cwd") or ""
    if raw and raw != "-" and Path(raw) != Path("/"):
        return concise_cwd(raw)

    transcript = data.get("transcript_path") or data.get("transcript")
    inferred = cwd_from_transcript(transcript) if isinstance(transcript, str) else ""
    if inferred:
        return concise_cwd(inferred)

    inferred = latest_active_workspace_root()
    if inferred:
        return concise_cwd(inferred)

    return concise_cwd(raw or "-")


def latest_prompt_history_task() -> str:
    # Fallback for Codex Desktop Stop events that arrive without transcript_path.
    # Only the newest prompt is used, and the value is never written to logs.
    path = CODEX_HOME / ".codex-global-state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        atoms = state.get("electron-persisted-atom-state", {})
        history = atoms.get("prompt-history", [])
        if not isinstance(history, list):
            return ""
        for item in reversed(history):
            if isinstance(item, str) and item.strip() and not is_system_scaffold_message(item):
                return item.strip()
    except Exception as exc:
        write_log("debug", "failed to read latest prompt history", error=str(exc))
    return ""


def task_description(data: dict, limit: int = 100) -> str:
    for key in (
        "prompt",
        "user_prompt",
        "task_prompt",
        "task_description",
        "task",
        "user_input",
        "last_user_message",
    ):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return compact_line(value, limit)

    transcript = data.get("transcript_path") or data.get("transcript")
    text = last_user_message_from_transcript(transcript) if isinstance(transcript, str) else ""
    if text:
        return compact_line(text, limit)

    text = latest_prompt_history_task()
    if text:
        return compact_line(text, limit)
    return ""


def display_host(env_values: dict | None = None) -> str:
    env_values = env_values or {}
    configured = (
        os.environ.get("CODEX_NOTIFY_HOST_LABEL", "").strip()
        or env_values.get("CODEX_NOTIFY_HOST_LABEL", "").strip()
        or env_values.get("FEISHU_DEVICE_NAME", "").strip()
    )
    if configured:
        return configured
    if socket.gethostname() == "SWQdeMacBook-Pro.local" and platform.machine() == "arm64":
        return "M4 MacBook Pro"
    return f"{socket.gethostname()} ({platform.machine()})"


def write_log(level: str, message: str, **fields) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "level": level,
        "message": message,
        **fields,
    }
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def read_stdin_json() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        write_log("error", "invalid hook json", error=str(exc), raw=raw[:MAX_FIELD_LEN])
        return {"_raw": raw, "_parse_error": str(exc)}


def load_env_file(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            values[key.strip()] = value
    except Exception as exc:
        write_log("error", "failed to read env file", path=str(path), error=str(exc))
    return values


def config_value(env_values: dict, key: str, default: str = "") -> str:
    return os.environ.get(key) or env_values.get(key) or default


def get_webhook_url(env_values: dict) -> str:
    return config_value(env_values, "FEISHU_WEBHOOK_URL")


def get_app_config(env_values: dict) -> dict:
    return {
        "app_id": config_value(env_values, "FEISHU_APP_ID"),
        "app_secret": config_value(env_values, "FEISHU_APP_SECRET"),
        "receive_id_type": config_value(env_values, "FEISHU_RECEIVE_ID_TYPE", "open_id"),
        "receive_id": config_value(env_values, "FEISHU_RECEIVE_ID"),
        "api_base": config_value(env_values, "FEISHU_API_BASE", DEFAULT_FEISHU_API_BASE).rstrip("/"),
    }


def app_config_missing(config: dict) -> list[str]:
    required = {
        "FEISHU_APP_ID": config.get("app_id"),
        "FEISHU_APP_SECRET": config.get("app_secret"),
        "FEISHU_RECEIVE_ID": config.get("receive_id"),
    }
    return [key for key, value in required.items() if not value]


def timeout_seconds(env_values: dict) -> int:
    raw = config_value(env_values, "FEISHU_TIMEOUT", "10")
    try:
        return max(3, min(60, int(raw)))
    except ValueError:
        return 10


def int_config(env_values: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    raw = config_value(env_values, key, str(default))
    try:
        return max(minimum, min(maximum, int(raw)))
    except ValueError:
        return default


def fingerprint(value: str) -> str:
    if not value:
        return "-"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def response_summary(payload) -> dict:
    if not isinstance(payload, dict):
        return {"body": compact(payload, 300)}
    summary = {}
    for key in ("code", "msg", "error", "error_description"):
        if key in payload:
            summary[key] = payload[key]
    if "data" in payload and isinstance(payload["data"], dict):
        data = payload["data"]
        for key in ("message_id", "request_id"):
            if key in data:
                summary[key] = data[key]
    return summary


def compact(value, limit: int = MAX_FIELD_LEN) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str, indent=2)
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "\n...<truncated>"


def nested_get(data: dict, path: list, default=None):
    cur = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def detect_failure(data: dict) -> tuple[bool, str]:
    response = data.get("tool_response")
    if response is None:
        return False, ""

    candidates = [
        nested_get(data, ["tool_response", "exit_code"]),
        nested_get(data, ["tool_response", "exitCode"]),
        nested_get(data, ["tool_response", "status"]),
        nested_get(data, ["tool_response", "code"]),
    ]
    for value in candidates:
        if isinstance(value, int) and value != 0:
            return True, f"工具退出码为 {value}"
        if isinstance(value, str) and value.lower() in {"error", "failed", "failure", "timeout"}:
            return True, f"工具状态为 {value}"

    text = compact(response, limit=4000).lower()
    high_confidence_keywords = [
        "traceback",
        "permission denied",
        "command not found",
        "no such file or directory",
        "timed out",
        "timeout",
        "fatal:",
        "exception",
        "modulenotfounderror",
        "importerror",
        "syntaxerror",
        "filenotfounderror",
        "segmentation fault",
    ]
    for keyword in high_confidence_keywords:
        if keyword in text:
            return True, f"工具输出包含高置信异常关键词：{keyword}"
    return False, ""


def detect_needs_human(data: dict) -> tuple[bool, str]:
    message = (data.get("last_assistant_message") or "").lower()
    if not message:
        return False, ""
    keywords = [
        "需要你",
        "请确认",
        "人工介入",
        "无法继续",
        "被阻塞",
        "卡住",
        "approval",
        "permission",
        "confirm",
        "blocked",
        "stuck",
        "manual intervention",
    ]
    for keyword in keywords:
        if keyword.lower() in message:
            return True, "可能需要人工介入"
    return False, ""


def describe_event(data: dict) -> tuple[bool, str, str]:
    event = data.get("hook_event_name") or "Unknown"
    tool = data.get("tool_name") or ""

    if event == "PermissionRequest":
        return True, "Codex 需要确认", "需要权限确认"

    if event == "Stop":
        needs_human, reason = detect_needs_human(data)
        if needs_human:
            return True, "Codex 可能需要人工介入", reason
        return True, "Codex 任务完成", "已结束"

    if event == "PostToolUse":
        write_log("debug", "post tool notification disabled", tool=tool)
        return False, "", ""

    return False, "", ""


def state_key(data: dict, title: str, reason: str) -> str:
    event = data.get("hook_event_name") or "Unknown"
    cwd = concise_cwd(data.get("cwd") or "-")
    tool = data.get("tool_name") or "-"
    return "|".join([event, title, reason or "-", cwd, tool])


def should_skip_notification(data: dict, title: str, reason: str, env_values: dict) -> tuple[bool, str]:
    event = data.get("hook_event_name") or "Unknown"
    cwd = data.get("cwd") or ""

    # Only throttle plain completion pushes. Approval and human-intervention
    # alerts must stay immediate.
    if event != "Stop" or title != "Codex 任务完成":
        return False, ""

    interval = int_config(
        env_values,
        "FEISHU_STOP_MIN_INTERVAL_SECONDS",
        DEFAULT_STOP_MIN_INTERVAL_SECONDS,
        0,
        86400,
    )
    root_window = int_config(
        env_values,
        "FEISHU_ROOT_STOP_DUPLICATE_WINDOW_SECONDS",
        DEFAULT_ROOT_STOP_DUPLICATE_WINDOW_SECONDS,
        0,
        300,
    )

    now = time.time()
    key = state_key(data, title, reason)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_FILE.with_suffix(".lock")

    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
        except Exception as exc:
            write_log("warning", "failed to read notify state; resetting", error=str(exc))
            state = {}

        # Codex Desktop can emit an extra Stop through the notify mux with
        # cwd="/". Suppress it only when it closely follows a real workspace
        # completion. A standalone cwd="/" task still deserves a completion push.
        if root_window > 0 and Path(cwd or "/") == Path("/"):
            for old_key, old_ts in state.items():
                parts = str(old_key).split("|")
                old_cwd = parts[3] if len(parts) >= 5 else ""
                try:
                    age = now - float(old_ts)
                except (TypeError, ValueError):
                    continue
                if (
                    len(parts) >= 5
                    and parts[0] == "Stop"
                    and parts[1] == "Codex 任务完成"
                    and old_cwd not in {"", "-", "/"}
                    and age < root_window
                ):
                    return True, f"skip duplicate root Stop notification within {root_window}s"

        last = float(state.get(key, 0) or 0)
        if interval > 0 and last and now - last < interval:
            return True, f"skip duplicate Stop notification within {interval}s"

        cutoff = now - 86400
        state = {k: v for k, v in state.items() if isinstance(v, (int, float)) and v >= cutoff}
        state[key] = now
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
        return False, ""


def concise_cwd(cwd: str) -> str:
    if not cwd or cwd == "-":
        return "-"
    try:
        return Path(cwd).name or cwd
    except Exception:
        return cwd


def build_stop_message(title: str, cwd: str, reason: str, last_message: str, device: str, task: str) -> str:
    summary = compact_line(last_message, 160) if last_message else reason or "已结束"
    lines = [
        f"【{title}】",
        f"时间：{now_local()}",
        f"设备：{device}",
        "事件：Stop",
        f"目录：{cwd}",
        f"任务：{task or '未记录'}",
        f"摘要：{summary}",
    ]
    return "\n".join(lines)


def build_message(data: dict, title: str, reason: str, env_values: dict | None = None) -> str:
    event = data.get("hook_event_name") or "Unknown"
    tool = data.get("tool_name") or "-"
    cwd = data.get("cwd") or "-"
    last_message = data.get("last_assistant_message") or ""
    device = display_host(env_values)

    if event == "Stop":
        return build_stop_message(title, display_cwd(data), reason, last_message, device, task_description(data, 100))

    lines = [
        f"【{title}】",
        f"时间：{now_local()}",
        f"设备：{device}",
        f"事件：{event}",
        f"目录：{display_cwd(data)}",
        f"原因：{reason or '-'}",
    ]
    if tool != "-":
        lines.insert(3, f"工具：{tool}")
    return "\n".join(lines)


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 10) -> tuple[int, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = raw
        return resp.status, parsed


def send_feishu_webhook(webhook_url: str, text: str, timeout: int) -> bool:
    payload = json.dumps(
        {"msg_type": "text", "content": {"text": text}},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            ok = 200 <= resp.status < 300
            write_log("info" if ok else "error", "feishu webhook response", status=resp.status, body=body[:MAX_FIELD_LEN])
            return ok
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        write_log("error", "feishu webhook http error", status=exc.code, body=body[:MAX_FIELD_LEN])
    except Exception as exc:
        write_log("error", "feishu webhook request failed", error=str(exc))
    return False


def get_tenant_access_token(config: dict, timeout: int) -> str:
    url = config["api_base"] + "/auth/v3/tenant_access_token/internal"
    try:
        status, payload = post_json(
            url,
            {"app_id": config["app_id"], "app_secret": config["app_secret"]},
            timeout=timeout,
        )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        write_log("error", "feishu token http error", status=exc.code, body=body[:MAX_FIELD_LEN])
        return ""
    except Exception as exc:
        write_log("error", "feishu token request failed", error=str(exc))
        return ""

    if not isinstance(payload, dict):
        write_log("error", "feishu token invalid response", status=status, summary=response_summary(payload))
        return ""

    if 200 <= status < 300 and payload.get("code") == 0 and payload.get("tenant_access_token"):
        write_log("info", "feishu token acquired", status=status, expire=payload.get("expire"))
        return str(payload["tenant_access_token"])

    write_log("error", "feishu token rejected", status=status, summary=response_summary(payload))
    return ""


def send_feishu_app_message(config: dict, text: str, timeout: int) -> bool:
    token = get_tenant_access_token(config, timeout)
    if not token:
        return False

    receive_id_type = config["receive_id_type"]
    receive_id = config["receive_id"]
    query = urllib.parse.urlencode({"receive_id_type": receive_id_type})
    url = config["api_base"] + "/im/v1/messages?" + query
    payload = {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
        "uuid": str(uuid.uuid4()),
    }
    try:
        status, response = post_json(
            url,
            payload,
            headers={"Authorization": "Bearer " + token},
            timeout=timeout,
        )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        write_log(
            "error",
            "feishu app message http error",
            status=exc.code,
            body=body[:MAX_FIELD_LEN],
            receive_id_type=receive_id_type,
            receiver=fingerprint(receive_id),
        )
        return False
    except Exception as exc:
        write_log(
            "error",
            "feishu app message request failed",
            error=str(exc),
            receive_id_type=receive_id_type,
            receiver=fingerprint(receive_id),
        )
        return False

    ok = isinstance(response, dict) and 200 <= status < 300 and response.get("code") == 0
    write_log(
        "info" if ok else "error",
        "feishu app message response",
        status=status,
        summary=response_summary(response),
        receive_id_type=receive_id_type,
        receiver=fingerprint(receive_id),
    )
    return ok


def send_notification(text: str, env_values: dict) -> tuple[bool, str]:
    timeout = timeout_seconds(env_values)
    webhook_url = get_webhook_url(env_values)
    app_config = get_app_config(env_values)
    missing = app_config_missing(app_config)
    mode = config_value(env_values, "FEISHU_DELIVERY_MODE", "auto").lower()

    if mode not in {"auto", "webhook", "app", "both"}:
        write_log("warning", "unknown delivery mode; fallback to auto", mode=mode)
        mode = "auto"

    methods: list[str] = []
    if mode == "both":
        if webhook_url:
            methods.append("webhook")
        if not missing:
            methods.append("app")
    elif mode == "webhook":
        methods.append("webhook")
    elif mode == "app":
        methods.append("app")
    elif not missing:
        methods.append("app")
    elif webhook_url:
        methods.append("webhook")

    if not methods:
        write_log(
            "warning",
            "feishu delivery not configured; notification skipped",
            missing_app_keys=missing,
            webhook_configured=bool(webhook_url),
        )
        return False, "none"

    sent = False
    attempted = []
    for method in methods:
        attempted.append(method)
        if method == "webhook":
            if not webhook_url:
                write_log("warning", "webhook delivery selected but webhook is empty")
                continue
            sent = send_feishu_webhook(webhook_url, text, timeout) or sent
        elif method == "app":
            if missing:
                write_log("warning", "app delivery selected but config is incomplete", missing_app_keys=missing)
                continue
            sent = send_feishu_app_message(app_config, text, timeout) or sent
    return sent, ",".join(attempted)


def main() -> int:
    data = read_stdin_json()
    should_notify, title, reason = describe_event(data)
    event = data.get("hook_event_name") or "Unknown"
    env_values = load_env_file(ENV_FILE)

    if should_notify:
        skip, skip_reason = should_skip_notification(data, title, reason, env_values)
        if skip:
            write_log("debug", "notification suppressed", event=event, title=title, reason=skip_reason)
        else:
            text = build_message(data, title, reason, env_values)
            sent, method = send_notification(text, env_values)
            write_log(
                "info" if sent else "warning",
                "notification processed",
                event=event,
                title=title,
                sent=sent,
                method=method,
            )
    else:
        write_log("debug", "notification not needed", event=event, tool=data.get("tool_name"))

    if event == "Stop":
        sys.stdout.write(json.dumps({"continue": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        write_log("error", "unhandled notify_feishu error", traceback=traceback.format_exc())
        if "Stop" in os.environ.get("CODEX_HOOK_EVENT_NAME", ""):
            sys.stdout.write(json.dumps({"continue": True}, ensure_ascii=False))
        raise SystemExit(0)
