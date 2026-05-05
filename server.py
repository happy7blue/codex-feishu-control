#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.server
import json
import os
import re
import secrets
import signal
import socketserver
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_STOPPED = "stopped"
STATUS_REJECTED = "rejected"

OPEN_API_BASE = "https://open.feishu.cn/open-apis"

CONFLICT_PROMPT_TEMPLATE = """你是一个任务冲突检测器。请判断新任务与当前正在运行的任务是否存在冲突。

判断标准（保守原则）：
- 只要不能确定安全并行，就判定为冲突
- 同一项目目录下，涉及写文件、修改代码、运行命令、安装依赖的任务，默认冲突
- 只有明确是只读性质的任务（查看日志、读文件、生成文档）才允许与写任务并行
- 不同项目目录的任务，只要操作性质不互相影响，可以并行

新任务：
项目：{new_project}
描述：{new_prompt}

当前正在运行的任务：
{running_list}

请只返回 JSON，格式如下，不要输出任何其他内容：
{{
  "conflict": true 或 false,
  "conflict_with": ["task_id1", "task_id2"],
  "reason": "判断理由"
}}
"""


def utc_now() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def expand_path(value: str) -> str:
    return str(Path(value).expanduser().resolve())


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def tail_text(path: Path, max_chars: int = 4000) -> str:
    if not path.exists():
        return ""
    max_bytes = max(max_chars * 4, 4096)
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes), os.SEEK_SET)
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    return text[-max_chars:]


def compact(text: str, limit: int = 1800) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def compact_line(text: str, limit: int = 120) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def parse_utc_timestamp(value: str) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def format_local_timestamp(value: str) -> str:
    parsed = parse_utc_timestamp(value)
    if parsed:
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return dt.datetime.now().astimezone().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def snapshot_similar(current: str, previous: str) -> bool:
    if current == previous:
        return True
    if not current or not previous:
        return False
    if abs(len(current) - len(previous)) >= 20:
        return False
    current_counts = Counter(current)
    previous_counts = Counter(previous)
    overlap = sum(min(current_counts[ch], previous_counts[ch]) for ch in current_counts)
    return overlap / max(len(current), len(previous), 1) >= 0.8


def safe_compare(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _format_conflict_task_list(running_tasks: List[Dict[str, Any]]) -> str:
    if not running_tasks:
        return "无"
    lines = []
    for task in running_tasks:
        lines.append(
            "\n".join(
                [
                    f"- task_id: {task.get('task_id', '')}",
                    f"  项目：{task.get('project_alias', '')} ({task.get('project_path', '')})",
                    f"  状态：{task.get('status', '')}",
                    f"  风险处理：{task.get('risk_action', '')}",
                    f"  描述：{task.get('prompt', '')}",
                ]
            )
        )
    return "\n".join(lines)


def _extract_json_object(text: str) -> Dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型未返回 JSON 对象")
    return json.loads(text[start : end + 1])


def _normalize_conflict_result(data: Dict[str, Any], running_tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    running_ids = {str(task.get("task_id", "")) for task in running_tasks if task.get("task_id")}
    raw_conflict = data.get("conflict", True)
    if isinstance(raw_conflict, bool):
        conflict = raw_conflict
    elif isinstance(raw_conflict, str) and raw_conflict.strip().lower() in ("true", "false"):
        conflict = raw_conflict.strip().lower() == "true"
    else:
        conflict = True
    raw_conflict_with = data.get("conflict_with") or []
    if not isinstance(raw_conflict_with, list):
        raw_conflict_with = [raw_conflict_with]
    conflict_with = [str(item) for item in raw_conflict_with if str(item) in running_ids]
    if conflict and not conflict_with:
        conflict_with = sorted(running_ids)
    return {
        "conflict": conflict,
        "conflict_with": conflict_with,
        "reason": str(data.get("reason") or "模型未给出理由"),
    }


def check_conflict(new_task: Dict[str, Any], running_tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not running_tasks:
        return {"conflict": False, "conflict_with": [], "reason": "当前没有正在运行的任务"}

    config = new_task.get("_config")
    if not isinstance(config, Config):
        task_ids = [str(task.get("task_id", "")) for task in running_tasks if task.get("task_id")]
        return {"conflict": True, "conflict_with": task_ids, "reason": "缺少模型配置，按保守原则判定为冲突"}

    new_project = f"{new_task.get('project_alias', '')} ({new_task.get('project_path', '')})"
    prompt = CONFLICT_PROMPT_TEMPLATE.format(
        new_project=new_project,
        new_prompt=new_task.get("prompt", ""),
        running_list=_format_conflict_task_list(running_tasks),
    )
    codex_bin = config.codex.get("bin", "/opt/homebrew/bin/codex")
    project_path = Path(str(new_task.get("project_path") or "."))
    cmd = [
        codex_bin,
        "exec",
        "--cd",
        str(project_path),
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "-c",
        'approval_policy="never"',
        "-",
    ]
    model = config.codex.get("model", "")
    if model:
        cmd[2:2] = ["--model", model]
    if config.codex.get("skip_git_repo_check", False):
        cmd.insert(-1, "--skip-git-repo-check")

    try:
        completed = subprocess.run(
            cmd,
            input=prompt,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=max(10, int(config.codex.get("conflict_timeout_seconds", 120))),
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"模型冲突检测退出码 {completed.returncode}: {compact(completed.stdout, 500)}")
        return _normalize_conflict_result(_extract_json_object(completed.stdout), running_tasks)
    except Exception as exc:
        task_ids = [str(task.get("task_id", "")) for task in running_tasks if task.get("task_id")]
        return {
            "conflict": True,
            "conflict_with": task_ids,
            "reason": f"冲突检测失败，按保守原则判定为冲突：{exc}",
        }


@dataclass
class FeishuMessage:
    chat_id: str
    sender_open_id: str
    text: str
    message_id: str


class Config:
    def __init__(self, raw: Dict[str, Any], config_path: Path):
        self.raw = raw
        self.config_path = config_path
        self.server = raw.get("server", {})
        self.feishu = raw.get("feishu", {})
        self.codex = raw.get("codex", {})
        self.security = raw.get("security", {})
        self.projects = raw.get("projects", {})

        self.host = self.server.get("host", "127.0.0.1")
        self.port = int(self.server.get("port", 8787))
        self.tasks_root = Path(expand_path(raw.get("tasks_root", "~/.codex_feishu_tasks")))
        self.event_mode = self.feishu.get("event_mode", "websocket")
        self.default_project_alias = raw.get("default_project_alias") or self.feishu.get("default_project_alias", "")
        self.allowed_open_ids = set(self.feishu.get("allowed_open_ids", []))
        self.dry_run = bool(self.feishu.get("dry_run", True))
        self.notify_on_start = bool(self.codex.get("notify_on_start", False))
        self.progress_interval_seconds = int(self.codex.get("progress_interval_seconds", 1800))
        self.progress_summary_window = max(0, int(self.codex.get("progress_summary_window", 0)))
        self.finish_summary_window = max(0, int(self.codex.get("finish_summary_window", 60)))

    @classmethod
    def from_file(cls, path: str) -> "Config":
        config_path = Path(path).expanduser().resolve()
        raw = load_json(config_path)
        return cls(raw, config_path)

    def project_path(self, alias: str) -> Optional[Path]:
        path = self.projects.get(alias)
        if not path:
            return None
        return Path(expand_path(path))


class FeishuClient:
    def __init__(self, config: Config):
        self.config = config
        self._token: Optional[str] = None
        self._token_expire_at = 0.0
        self._lock = threading.Lock()

    def _post_json(self, url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    def tenant_access_token(self) -> str:
        if self.config.dry_run:
            return "dry-run-token"
        with self._lock:
            if self._token and time.time() < self._token_expire_at - 60:
                return self._token
            app_id = self.config.feishu.get("app_id", "")
            app_secret = self.config.feishu.get("app_secret", "")
            if not app_id or not app_secret:
                raise RuntimeError("缺少 feishu.app_id 或 feishu.app_secret")
            url = f"{OPEN_API_BASE}/auth/v3/tenant_access_token/internal"
            resp = self._post_json(
                url,
                {"app_id": app_id, "app_secret": app_secret},
                {"Content-Type": "application/json; charset=utf-8"},
            )
            if resp.get("code") != 0:
                raise RuntimeError(f"获取 tenant_access_token 失败: {resp}")
            self._token = resp["tenant_access_token"]
            self._token_expire_at = time.time() + int(resp.get("expire", 7200))
            return self._token

    def send_text(self, chat_id: str, text: str) -> None:
        text = compact(text, 3500)
        if self.config.dry_run:
            print(f"[dry-run] send to {chat_id}:\n{text}\n", flush=True)
            return
        token = self.tenant_access_token()
        url = f"{OPEN_API_BASE}/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        resp = self._post_json(url, payload, headers)
        if resp.get("code") != 0:
            raise RuntimeError(f"发送飞书消息失败: {resp}")


class RiskGuard:
    HARD_REJECT = [
        r"读取.*(密钥|secret|token|password|密码|私钥)",
        r"(cat|open|查看|打印).*(\.env|id_rsa|keychain|钥匙串)",
        r"dangerously-bypass-approvals-and-sandbox",
        r"\bfull-auto\b",
    ]
    HIGH_RISK = [
        r"\brm\s+-rf\b",
        r"\bgit\s+push\b",
        r"\bsudo\b",
        r"\b(chmod|chown)\b",
        r"\b(brew|npm|pnpm|yarn|pip|uv)\s+(install|add|upgrade)\b",
        r"(删除|移除|清空|覆盖|批量|重装|系统设置|开机项|LaunchDaemon)",
        r"(install|delete|remove|overwrite).*(dependency|package|file|directory)",
    ]

    def __init__(self, policy: str):
        self.policy = policy

    def classify(self, text: str) -> Tuple[str, str]:
        lowered = text.lower()
        for pattern in self.HARD_REJECT:
            if re.search(pattern, lowered, flags=re.IGNORECASE):
                return "reject", f"命中拒绝规则: {pattern}"
        for pattern in self.HIGH_RISK:
            if re.search(pattern, lowered, flags=re.IGNORECASE):
                if self.policy == "reject":
                    return "reject", f"命中高风险规则: {pattern}"
                return "plan", f"命中高风险规则: {pattern}"
        return "run", "低风险"


class TaskManager:
    def __init__(self, config: Config, feishu: FeishuClient):
        self.config = config
        self.feishu = feishu
        self.guard = RiskGuard(config.security.get("high_risk_policy", "plan"))
        self._lock = threading.Lock()
        self._processes: Dict[str, subprocess.Popen] = {}
        self._finish_lock = threading.Lock()
        self._pending_finished: Dict[str, List[Dict[str, Any]]] = {}
        self._finish_timers: Dict[str, threading.Timer] = {}
        self._finish_timer_versions: Dict[str, int] = {}
        self._queue_lock = threading.Lock()
        self.pending_queue: List[Dict[str, Any]] = []
        self._queue_processing = False
        self.config.tasks_root.mkdir(parents=True, exist_ok=True)
        self._mark_orphan_running_tasks()

    def _task_dir(self, task_id: str) -> Path:
        return self.config.tasks_root / task_id

    def _meta_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "meta.json"

    def _log_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "output.log"

    def _last_message_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "last_message.txt"

    def _load_meta(self, task_id: str) -> Optional[Dict[str, Any]]:
        path = self._meta_path(task_id)
        if not path.exists():
            return None
        return load_json(path)

    def _save_meta(self, task_id: str, meta: Dict[str, Any]) -> None:
        write_json_atomic(self._meta_path(task_id), meta)

    def _append_log(self, task_id: str, text: str) -> None:
        with self._log_path(task_id).open("a", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")

    def _mark_orphan_running_tasks(self) -> None:
        for meta_path in self.config.tasks_root.glob("*/meta.json"):
            try:
                meta = load_json(meta_path)
                if meta.get("status") == STATUS_RUNNING:
                    meta["status"] = STATUS_FAILED
                    meta["finished_at"] = utc_now()
                    meta["error"] = "服务重启后无法继续跟踪旧进程"
                    write_json_atomic(meta_path, meta)
            except Exception:
                traceback.print_exc()

    def latest_task_for_chat(self, chat_id: str, sender_open_id: str = "") -> Optional[Dict[str, Any]]:
        tasks = self.list_tasks(limit=200)
        for meta in tasks:
            if meta.get("chat_id") != chat_id:
                continue
            if sender_open_id and meta.get("sender_open_id") != sender_open_id:
                continue
            return meta
        return None

    def latest_running_task_for_chat(self, chat_id: str, sender_open_id: str = "") -> Optional[Dict[str, Any]]:
        tasks = self.list_tasks(limit=200)
        for meta in tasks:
            if meta.get("chat_id") != chat_id:
                continue
            if sender_open_id and meta.get("sender_open_id") != sender_open_id:
                continue
            if meta.get("status") == STATUS_RUNNING:
                return meta
        return None

    def list_tasks(self, limit: int = 10) -> List[Dict[str, Any]]:
        items = []
        for meta_path in self.config.tasks_root.glob("*/meta.json"):
            try:
                items.append(load_json(meta_path))
            except Exception:
                continue
        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return items[:limit]

    def _running_tasks(self) -> List[Dict[str, Any]]:
        items = []
        for meta_path in self.config.tasks_root.glob("*/meta.json"):
            try:
                meta = load_json(meta_path)
            except Exception:
                continue
            if meta.get("status") == STATUS_RUNNING:
                items.append(meta)
        items.sort(key=lambda x: x.get("created_at", ""))
        return items

    def status_text(self, task_id: Optional[str] = None) -> str:
        if task_id:
            meta = self._load_meta(task_id)
            if not meta:
                return f"未找到任务: {task_id}"
            return self._format_status(meta)
        tasks = self.list_tasks()
        if not tasks:
            return "暂无任务。"
        lines = ["最近任务："]
        for meta in tasks:
            lines.append(self._format_status(meta, one_line=True))
        return "\n".join(lines)

    def _format_status(self, meta: Dict[str, Any], one_line: bool = False) -> str:
        task_id = meta.get("task_id", "")
        status = meta.get("status", "")
        alias = meta.get("project_alias", "")
        action = meta.get("risk_action", "")
        created = meta.get("created_at", "")
        finished = meta.get("finished_at", "")
        code = meta.get("return_code")
        if one_line:
            suffix = f", rc={code}" if code is not None else ""
            return f"- {task_id} [{status}] {alias} {action}{suffix}"
        fields = [
            f"任务: {task_id}",
            f"状态: {status}",
            f"项目: {alias}",
            f"风险处理: {action}",
            f"创建: {created}",
        ]
        if finished:
            fields.append(f"结束: {finished}")
        if code is not None:
            fields.append(f"退出码: {code}")
        if meta.get("error"):
            fields.append(f"错误: {meta['error']}")
        return "\n".join(fields)

    def log_text(self, task_id: str, chars: int = 4000) -> str:
        meta = self._load_meta(task_id)
        if not meta:
            return f"未找到任务: {task_id}"
        text = tail_text(self._log_path(task_id), chars)
        if not text:
            return f"任务 {task_id} 暂无日志。"
        return f"任务 {task_id} 最近日志：\n{text}"

    def stop(self, task_id: str) -> str:
        with self._lock:
            proc = self._processes.get(task_id)
        meta = self._load_meta(task_id)
        if not meta:
            return f"未找到任务: {task_id}"
        if not proc or proc.poll() is not None:
            return f"任务 {task_id} 当前不可停止，状态是 {meta.get('status')}。"
        meta["stop_requested"] = True
        self._save_meta(task_id, meta)
        self._terminate_process(proc)
        return f"已请求停止任务: {task_id}"

    def start(self, project_alias: str, user_prompt: str, chat_id: str, sender_open_id: str) -> str:
        project_path = self.config.project_path(project_alias)
        if not project_path:
            return f"未知项目别名: {project_alias}\n可用项目: {', '.join(sorted(self.config.projects.keys()))}"
        if not project_path.exists() or not project_path.is_dir():
            return f"项目目录不存在或不是目录: {project_path}"

        action, reason = self.guard.classify(user_prompt)
        new_task = {
            "queue_id": secrets.token_hex(6),
            "project_alias": project_alias,
            "project_path": str(project_path),
            "prompt": user_prompt,
            "chat_id": chat_id,
            "sender_open_id": sender_open_id,
            "risk_action": action,
            "risk_reason": reason,
            "queued_at": utc_now(),
            "_config": self.config,
        }

        if action == "reject":
            task_id = self._reject_task(new_task)
            return f"已拒绝高风险任务。\n任务: {task_id}\n原因: {reason}"

        running_tasks = self._running_tasks()
        if not running_tasks:
            task_id, error = self._start_task_now(new_task)
            if error:
                return f"启动 Codex 失败。\n任务: {task_id}\n错误: {error}"
            return f"✅ 无冲突，任务已直接启动\n任务: {task_id}"

        conflict = check_conflict(new_task, running_tasks)
        if not conflict.get("conflict"):
            task_id, error = self._start_task_now(new_task)
            if error:
                return f"启动 Codex 失败。\n任务: {task_id}\n错误: {error}"
            return (
                "✅ 经检测无冲突，任务已直接启动\n"
                f"检测理由：{conflict.get('reason')}\n"
                f"任务: {task_id}"
            )

        with self._queue_lock:
            self.pending_queue.append(dict(new_task))
        conflict_with = ", ".join(conflict.get("conflict_with") or []) or "未指定"
        return (
            "⏳ 检测到冲突，任务已进入队列\n"
            f"冲突任务：{conflict_with}\n"
            f"原因：{conflict.get('reason')}\n"
            "将在冲突任务完成后自动启动"
        )

    def _reject_task(self, task: Dict[str, Any]) -> str:
        task_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        task_dir = self._task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=False)
        meta = {
            "task_id": task_id,
            "status": STATUS_REJECTED,
            "created_at": utc_now(),
            "project_alias": task.get("project_alias"),
            "project_path": task.get("project_path"),
            "prompt": task.get("prompt"),
            "chat_id": task.get("chat_id"),
            "sender_open_id": task.get("sender_open_id"),
            "risk_action": task.get("risk_action"),
            "risk_reason": task.get("risk_reason"),
            "return_code": None,
            "progress_notifications_sent": 0,
            "finished_at": utc_now(),
            "error": task.get("risk_reason"),
        }
        self._save_meta(task_id, meta)
        self._append_log(
            task_id,
            f"[{utc_now()}] task={task_id} action={task.get('risk_action')} reason={task.get('risk_reason')}",
        )
        return task_id

    def _start_task_now(self, task: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        project_alias = str(task.get("project_alias") or "")
        project_path = Path(str(task.get("project_path") or ""))
        user_prompt = str(task.get("prompt") or "")
        action = str(task.get("risk_action") or "run")
        reason = str(task.get("risk_reason") or "")
        task_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        task_dir = self._task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=False)

        meta = {
            "task_id": task_id,
            "status": STATUS_RUNNING,
            "created_at": utc_now(),
            "project_alias": project_alias,
            "project_path": str(project_path),
            "prompt": user_prompt,
            "chat_id": task.get("chat_id"),
            "sender_open_id": task.get("sender_open_id"),
            "risk_action": action,
            "risk_reason": reason,
            "return_code": None,
            "progress_notifications_sent": 0,
        }
        self._save_meta(task_id, meta)
        self._append_log(task_id, f"[{utc_now()}] task={task_id} action={action} reason={reason}")

        prompt = self._build_codex_prompt(project_alias, project_path, user_prompt, action, reason)
        cmd = self._build_codex_cmd(project_path, action, task_id)
        try:
            log_file = self._log_path(task_id).open("a", encoding="utf-8")
            proc = subprocess.Popen(
                cmd,
                cwd=str(project_path),
                stdin=subprocess.PIPE,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except Exception as exc:
            meta["status"] = STATUS_FAILED
            meta["finished_at"] = utc_now()
            meta["error"] = str(exc)
            self._save_meta(task_id, meta)
            return task_id, str(exc)

        with self._lock:
            self._processes[task_id] = proc
        watcher = threading.Thread(
            target=self._watch_task,
            args=(task_id, proc, prompt, log_file),
            name=f"task-{task_id}",
            daemon=True,
        )
        watcher.start()
        return task_id, None

    def _process_pending_queue_async(self) -> None:
        with self._queue_lock:
            if self._queue_processing or not self.pending_queue:
                return
            self._queue_processing = True
        worker = threading.Thread(target=self._process_pending_queue, name="pending-queue", daemon=True)
        worker.start()

    def _process_pending_queue(self) -> None:
        try:
            with self._queue_lock:
                if not self.pending_queue:
                    return
                task = dict(self.pending_queue[0])
            task["_config"] = self.config
            running_tasks = self._running_tasks()
            if running_tasks:
                conflict = check_conflict(task, running_tasks)
            else:
                conflict = {"conflict": False, "conflict_with": [], "reason": "当前没有正在运行的任务"}

            if conflict.get("conflict"):
                conflict_with = ", ".join(conflict.get("conflict_with") or []) or "未指定"
                self._send_queue_text(
                    task,
                    "⏳ 队列任务仍在等待\n"
                    f"冲突任务：{conflict_with}",
                )
                return

            with self._queue_lock:
                if not self.pending_queue or self.pending_queue[0].get("queue_id") != task.get("queue_id"):
                    return
                self.pending_queue.pop(0)

            task_id, error = self._start_task_now(task)
            if error:
                self._send_queue_text(
                    task,
                    "队列任务自动启动失败\n"
                    f"项目：{task.get('project_alias')}\n"
                    f"任务：{task.get('prompt')}\n"
                    f"错误：{error}",
                )
                return
            self._send_queue_text(
                task,
                "🚀 队列任务已自动启动\n"
                f"项目：{task.get('project_alias')}\n"
                f"任务：{task.get('prompt')}",
            )
        finally:
            with self._queue_lock:
                self._queue_processing = False

    def _send_queue_text(self, task: Dict[str, Any], text: str) -> None:
        chat_id = task.get("chat_id")
        if not chat_id:
            return
        try:
            self.feishu.send_text(str(chat_id), text)
        except Exception:
            traceback.print_exc()

    def _build_codex_cmd(self, project_path: Path, action: str, task_id: str) -> List[str]:
        codex_bin = self.config.codex.get("bin", "/opt/homebrew/bin/codex")
        sandbox = "read-only" if action == "plan" else self.config.codex.get("sandbox", "workspace-write")
        cmd = [
            codex_bin,
            "exec",
            "--cd",
            str(project_path),
            "--sandbox",
            sandbox,
            "--color",
            "never",
            "--output-last-message",
            str(self._last_message_path(task_id)),
            "-c",
            'approval_policy="never"',
            "-",
        ]
        model = self.config.codex.get("model", "")
        if model:
            cmd[2:2] = ["--model", model]
        if self.config.codex.get("skip_git_repo_check", False):
            cmd.insert(-1, "--skip-git-repo-check")
        return cmd

    def _build_codex_prompt(
        self,
        project_alias: str,
        project_path: Path,
        user_prompt: str,
        action: str,
        reason: str,
    ) -> str:
        if action == "plan":
            return f"""你是由“Codex 飞书控制服务”启动的本机 Codex CLI。
项目别名：{project_alias}
项目路径：{project_path}

这条任务被服务层判定为高风险：{reason}
第一版飞书控制服务不支持交互式 approve/reject。
请只做只读分析并输出中文执行计划、风险点、需要人工确认的命令清单。
不要修改文件，不要安装依赖，不要删除文件，不要推送 git，不要读取密钥。

用户任务：
{user_prompt}
"""
        return f"""你是由“Codex 飞书控制服务”启动的本机 Codex CLI。
项目别名：{project_alias}
项目路径：{project_path}

请在该白名单项目目录内完成用户任务，并在最终回复中用简体中文简洁总结：
1. 做了什么；
2. 验证结果；
3. 如有失败，说明失败原因和下一步建议。

安全边界：
- 不要使用 full-auto 或 danger-full-access；
- 不要读取密钥、私钥、密码、token；
- 不要执行 git push、系统设置、批量删除、批量覆盖或安装依赖；如果确实需要，请停止并输出需要人工确认的计划。

用户任务：
{user_prompt}
"""

    def _watch_task(
        self,
        task_id: str,
        proc: subprocess.Popen,
        prompt: str,
        log_file: Any,
    ) -> None:
        timeout = int(self.config.codex.get("timeout_seconds", 1800))
        progress_interval = max(0, int(self.config.progress_interval_seconds))
        progress_window = max(0, int(self.config.progress_summary_window))
        started_at = time.time()
        next_progress_at = started_at + progress_interval if progress_interval else 0
        pending_progress_elapsed: Optional[float] = None
        pending_progress_due = 0.0
        try:
            if proc.stdin:
                proc.stdin.write(prompt)
                proc.stdin.close()
            while True:
                now = time.time()
                return_code = proc.poll()
                if return_code is not None:
                    break
                elapsed = now - started_at
                if timeout > 0 and elapsed >= timeout:
                    self._terminate_process(proc, kill=True)
                    self._finish_task(task_id, STATUS_TIMEOUT, proc.returncode, "任务超时")
                    return
                if progress_window and pending_progress_elapsed is not None and now >= pending_progress_due:
                    self._notify_progress(task_id, pending_progress_elapsed)
                    pending_progress_elapsed = None
                    pending_progress_due = 0.0
                if progress_interval and now >= next_progress_at:
                    if progress_window:
                        pending_progress_elapsed = elapsed
                        if not pending_progress_due:
                            pending_progress_due = now + progress_window
                    else:
                        self._notify_progress(task_id, elapsed)
                    next_progress_at += progress_interval
                time.sleep(2)
            meta = self._load_meta(task_id) or {}
            if meta.get("stop_requested"):
                self._finish_task(task_id, STATUS_STOPPED, proc.returncode, "用户停止任务")
            elif proc.returncode == 0:
                self._finish_task(task_id, STATUS_SUCCEEDED, proc.returncode, None)
            else:
                self._finish_task(task_id, STATUS_FAILED, proc.returncode, f"Codex 退出码 {proc.returncode}")
        except Exception as exc:
            self._append_log(task_id, traceback.format_exc())
            self._finish_task(task_id, STATUS_FAILED, proc.returncode, str(exc))
        finally:
            try:
                log_file.close()
            except Exception:
                pass
            with self._lock:
                self._processes.pop(task_id, None)

    def _notify_progress(self, task_id: str, elapsed_seconds: float) -> None:
        meta = self._load_meta(task_id) or {}
        chat_id = meta.get("chat_id")
        if not chat_id or meta.get("status") != STATUS_RUNNING:
            return
        snapshot = tail_text(self._log_path(task_id), 200).strip()
        previous_snapshot = str(meta.get("last_progress_snapshot") or "")
        if previous_snapshot and snapshot_similar(snapshot, previous_snapshot):
            return
        sent = int(meta.get("progress_notifications_sent") or 0) + 1
        meta["progress_notifications_sent"] = sent
        meta["last_progress_at"] = utc_now()
        meta["last_progress_snapshot"] = snapshot
        self._save_meta(task_id, meta)
        minutes = int(elapsed_seconds // 60)
        log_tail = compact(tail_text(self._log_path(task_id), 300), 300)
        if previous_snapshot:
            status_change = "输出有新增内容"
        elif snapshot:
            status_change = "首次进展汇总，输出有新增内容"
        else:
            status_change = "首次进展汇总，输出暂无内容，任务仍在运行"
        text = (
            f"【进展汇总】{meta.get('project_alias')}\n"
            f"已运行：约 {minutes} 分钟\n"
            f"通知次数：第 {sent} 次\n"
            f"状态变化：{status_change}\n"
            "最近输出（节选）：\n"
            f"{log_tail or '暂无输出。'}"
        )
        try:
            self.feishu.send_text(chat_id, text)
        except Exception:
            self._append_log(task_id, "发送进展通知失败：\n" + traceback.format_exc())

    def _finish_task(self, task_id: str, status: str, return_code: Optional[int], error: Optional[str]) -> None:
        meta = self._load_meta(task_id) or {}
        meta["status"] = status
        meta["return_code"] = return_code
        meta["finished_at"] = utc_now()
        if error:
            meta["error"] = error
        self._save_meta(task_id, meta)
        self._append_log(task_id, f"[{utc_now()}] finished status={status} rc={return_code} error={error or ''}")
        self._queue_finished_notification(task_id, meta)
        self._process_pending_queue_async()

    def _queue_finished_notification(self, task_id: str, meta: Dict[str, Any]) -> None:
        chat_id = str(meta.get("chat_id") or "").strip()
        if not chat_id:
            return
        window = max(0, int(self.config.finish_summary_window))
        if window == 0:
            self._notify_finished(task_id, meta)
            return
        with self._finish_lock:
            self._pending_finished.setdefault(chat_id, []).append(
                {
                    "task_id": task_id,
                    "meta": dict(meta),
                }
            )
            existing_timer = self._finish_timers.get(chat_id)
            if existing_timer:
                existing_timer.cancel()
            version = self._finish_timer_versions.get(chat_id, 0) + 1
            self._finish_timer_versions[chat_id] = version
            timer = threading.Timer(window, self._flush_finished_notifications, args=(chat_id, version))
            timer.daemon = True
            self._finish_timers[chat_id] = timer
            timer.start()

    def _flush_finished_notifications(self, chat_id: str, version: Optional[int] = None) -> None:
        with self._finish_lock:
            if version is not None and self._finish_timer_versions.get(chat_id) != version:
                return
            entries = self._pending_finished.pop(chat_id, [])
            self._finish_timers.pop(chat_id, None)
            self._finish_timer_versions.pop(chat_id, None)
        if not entries:
            return
        if len(entries) == 1:
            entry = entries[0]
            self._notify_finished(entry["task_id"], entry["meta"])
            return
        self._notify_finished_batch(chat_id, entries)

    def _notify_finished_batch(self, chat_id: str, entries: List[Dict[str, Any]]) -> None:
        latest_meta = entries[-1]["meta"]
        lines = [
            f"【任务批量完成】共 {len(entries)} 个任务",
            f"时间：{self._finished_time(latest_meta)}",
        ]
        for entry in entries:
            lines.append(self._format_finished_batch_line(entry["task_id"], entry["meta"]))
        text = "\n".join(lines)
        try:
            self.feishu.send_text(chat_id, text)
        except Exception:
            for entry in entries:
                self._append_log(entry["task_id"], "发送批量完成通知失败：\n" + traceback.format_exc())

    def _format_finished_batch_line(self, task_id: str, meta: Dict[str, Any]) -> str:
        alias = meta.get("project_alias") or "-"
        status = meta.get("status")
        task_text = self._task_description(meta, 50)
        task_suffix = f" · {task_text}" if task_text else ""
        if status == STATUS_SUCCEEDED:
            return f"✅ {task_id} {alias} — 成功{task_suffix}"
        if status == STATUS_FAILED:
            error = compact_line(meta.get("error") or "未知错误", 140)
            return f"❌ {task_id} {alias} — 失败：{error}{task_suffix}"
        if status == STATUS_TIMEOUT:
            return f"⏱ {task_id} {alias} — 超时{task_suffix}"
        if status == STATUS_STOPPED:
            return f"🛑 {task_id} {alias} — 已停止{task_suffix}"
        return f"{task_id} {alias} — {status or '已结束'}{task_suffix}"

    def _notify_finished(self, task_id: str, meta: Dict[str, Any]) -> None:
        chat_id = meta.get("chat_id")
        if not chat_id:
            return
        title_map = {
            STATUS_SUCCEEDED: "任务完成",
            STATUS_FAILED: "任务失败",
            STATUS_TIMEOUT: "任务超时",
            STATUS_STOPPED: "任务已停止",
        }
        title = title_map.get(meta.get("status"), "任务结束")
        summary = self._finished_summary(task_id)
        minutes = self._finished_minutes(meta)
        text = (
            f"【{title}】\n"
            f"时间：{self._finished_time(meta)}\n"
            f"项目：{meta.get('project_alias')}\n"
            f"任务：{self._task_description(meta, 100) or '未记录'}\n"
            f"耗时：约 {minutes} 分钟\n"
            f"结果：{meta.get('status')}\n"
            f"摘要：{summary}"
        )
        try:
            self.feishu.send_text(chat_id, text)
        except Exception:
            self._append_log(task_id, "发送完成通知失败：\n" + traceback.format_exc())

    def _finished_summary(self, task_id: str) -> str:
        last_message = tail_text(self._last_message_path(task_id), 2400).strip()
        if last_message:
            return compact(last_message, 2400)
        log_tail = tail_text(self._log_path(task_id), 300).strip()
        if log_tail:
            return compact(log_tail, 300)
        return "无输出"

    def _finished_time(self, meta: Dict[str, Any]) -> str:
        return format_local_timestamp(str(meta.get("finished_at") or ""))

    def _task_description(self, meta: Dict[str, Any], limit: int) -> str:
        return compact_line(str(meta.get("prompt") or "").strip(), limit)

    def _finished_minutes(self, meta: Dict[str, Any]) -> int:
        created_at = parse_utc_timestamp(str(meta.get("created_at") or ""))
        finished_at = parse_utc_timestamp(str(meta.get("finished_at") or ""))
        if not created_at or not finished_at:
            return 0
        elapsed = max(0.0, (finished_at - created_at).total_seconds())
        if elapsed <= 0:
            return 0
        return max(1, int((elapsed + 59) // 60))

    def _terminate_process(self, proc: subprocess.Popen, kill: bool = False) -> None:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL if kill else signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            try:
                proc.kill() if kill else proc.terminate()
            except Exception:
                pass
        if not kill:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._terminate_process(proc, kill=True)


class CommandRouter:
    def __init__(self, config: Config, tasks: TaskManager, feishu: FeishuClient):
        self.config = config
        self.tasks = tasks
        self.feishu = feishu
        self.state_path = self.config.tasks_root / "session_state.json"
        self._state_lock = threading.Lock()
        self._state = self._load_state()

    def handle_message(self, msg: FeishuMessage) -> None:
        if self.config.allowed_open_ids and msg.sender_open_id not in self.config.allowed_open_ids:
            self.feishu.send_text(msg.chat_id, "你不在允许使用 Codex 控制服务的用户白名单中。")
            return
        text = normalize_text(msg.text)
        if not text:
            return
        try:
            reply = self.dispatch(text, msg)
        except Exception as exc:
            reply = f"处理指令失败: {exc}"
            traceback.print_exc()
        if reply:
            self.feishu.send_text(msg.chat_id, reply)

    def dispatch(self, text: str, msg: FeishuMessage) -> Optional[str]:
        if text.startswith("/"):
            text = text[1:]
        if text in ("help", "帮助", "？", "?"):
            return HELP_TEXT
        if text in ("项目", "当前项目"):
            return self._project_text(msg)
        if text in ("项目列表", "白名单", "项目白名单"):
            return self._projects_text()
        switch = self._parse_project_switch(text)
        if switch:
            return self._switch_project(msg, switch)
        if text in ("status", "状态", "查状态", "任务状态"):
            task_id = self._current_task_id(msg)
            return self.tasks.status_text(task_id) if task_id else self.tasks.status_text()
        if text.startswith("status "):
            return self.tasks.status_text(text.split(maxsplit=1)[1].strip())
        if text.startswith("状态 "):
            return self.tasks.status_text(text.split(maxsplit=1)[1].strip())
        if text in ("stop", "停止", "停下", "终止"):
            task_id = self._current_running_task_id(msg) or self._current_task_id(msg)
            return self.tasks.stop(task_id) if task_id else "当前没有可停止的任务。"
        if text.startswith("stop "):
            return self.tasks.stop(text.split(maxsplit=1)[1].strip())
        if text.startswith("停止 "):
            return self.tasks.stop(text.split(maxsplit=1)[1].strip())
        if text in ("log", "日志", "看日志", "最近日志"):
            task_id = self._current_task_id(msg)
            return self.tasks.log_text(task_id) if task_id else "当前还没有任务日志。"
        if text.startswith("log "):
            return self.tasks.log_text(text.split(maxsplit=1)[1].strip())
        if text.startswith("日志 "):
            return self.tasks.log_text(text.split(maxsplit=1)[1].strip())
        if text.startswith("run "):
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                return "格式错误：run <项目别名> <任务内容>"
            return self._start_task(parts[1], parts[2], msg)

        project_alias, prompt = self._natural_task(text, msg)
        return self._start_task(project_alias, prompt, msg)

    def _start_task(self, project_alias: str, prompt: str, msg: FeishuMessage) -> Optional[str]:
        reply = self.tasks.start(project_alias, prompt, msg.chat_id, msg.sender_open_id)
        task_id = self._extract_task_id(reply)
        started = bool(
            task_id
            and (
                reply.startswith("已启动任务:")
                or reply.startswith("已启动计划模式任务:")
                or "已直接启动" in reply
            )
        )
        if started:
            self._set_current_project(msg, project_alias)
            self._set_current_task(msg, task_id)
            print(reply, flush=True)
        return reply

    def _natural_task(self, text: str, msg: Optional[FeishuMessage] = None) -> Tuple[str, str]:
        parts = text.split(maxsplit=1)
        if parts and parts[0] in self.config.projects and len(parts) == 2:
            return parts[0], parts[1].strip()
        alias = self._current_project(msg) if msg else self.config.default_project_alias
        if not alias:
            existing = [name for name, path in self.config.projects.items() if Path(expand_path(path)).is_dir()]
            if len(existing) == 1:
                alias = existing[0]
        if not alias:
            aliases = ", ".join(sorted(self.config.projects.keys()))
            raise RuntimeError(f"没有配置默认项目。请在 config.json 设置 default_project_alias，或用：run <项目别名> <任务内容>。可用项目：{aliases}")
        return alias, text

    def _session_key(self, msg: FeishuMessage) -> str:
        return f"{msg.chat_id}:{msg.sender_open_id}"

    def _load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            return load_json(self.state_path)
        except Exception:
            traceback.print_exc()
            return {}

    def _save_state(self) -> None:
        self.config.tasks_root.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.state_path, self._state)

    def _session(self, msg: FeishuMessage) -> Dict[str, Any]:
        key = self._session_key(msg)
        with self._state_lock:
            session = self._state.setdefault(key, {})
            return dict(session)

    def _update_session(self, msg: FeishuMessage, **values: Any) -> None:
        key = self._session_key(msg)
        with self._state_lock:
            session = self._state.setdefault(key, {})
            session.update(values)
            session["updated_at"] = utc_now()
            self._save_state()

    def _set_current_project(self, msg: FeishuMessage, project_alias: str) -> None:
        self._update_session(msg, current_project=project_alias)

    def _set_current_task(self, msg: FeishuMessage, task_id: str) -> None:
        self._update_session(msg, current_task_id=task_id)

    def _current_task_id(self, msg: FeishuMessage) -> Optional[str]:
        session = self._session(msg)
        task_id = session.get("current_task_id")
        if task_id:
            return task_id
        latest = self.tasks.latest_task_for_chat(msg.chat_id, msg.sender_open_id)
        return latest.get("task_id") if latest else None

    def _current_running_task_id(self, msg: FeishuMessage) -> Optional[str]:
        latest = self.tasks.latest_running_task_for_chat(msg.chat_id, msg.sender_open_id)
        return latest.get("task_id") if latest else None

    def _current_project(self, msg: FeishuMessage) -> str:
        session = self._session(msg)
        alias = session.get("current_project")
        if alias in self.config.projects:
            return alias
        return self.config.default_project_alias

    def _parse_project_switch(self, text: str) -> Optional[str]:
        explicit = re.match(r"^(?:切换到|切到|切换项目到|切换项目|换到|换项目到)\s*(\S+)\s*$", text)
        if explicit:
            return explicit.group(1)
        soft = re.match(r"^(?:使用|用)\s*(\S+)\s*(?:项目)?\s*$", text)
        if soft and soft.group(1) in self.config.projects:
            return soft.group(1)
        return None

    def _switch_project(self, msg: FeishuMessage, project_alias: str) -> str:
        project_path = self.config.project_path(project_alias)
        if not project_path:
            return f"未知项目：{project_alias}\n可用项目：{', '.join(sorted(self.config.projects.keys()))}"
        if not project_path.exists() or not project_path.is_dir():
            return f"项目目录不存在：{project_alias}"
        self._set_current_project(msg, project_alias)
        return f"已切换到项目：{project_alias}"

    def _project_text(self, msg: FeishuMessage) -> str:
        alias = self._current_project(msg)
        if alias:
            return f"当前项目：{alias}"
        return self._projects_text()

    def _projects_text(self) -> str:
        lines = ["可用项目："]
        for alias in sorted(self.config.projects.keys()):
            path = self.config.project_path(alias)
            status = "可用" if path and path.is_dir() else "目录不存在"
            lines.append(f"- {alias}: {status}")
        return "\n".join(lines)

    def _extract_task_id(self, reply: str) -> Optional[str]:
        match = re.search(r"任务:\s*([0-9]{8}-[0-9]{6}-[a-f0-9]+)", reply)
        return match.group(1) if match else None


HELP_TEXT = """直接说你要做什么即可，我会把消息交给默认白名单项目里的 Codex。

可选控制指令：
状态
日志
停止
当前项目
项目列表
切换到 <项目别名>

如果要临时指定项目：
<项目别名> <任务内容>
"""


def normalize_text(text: str) -> str:
    text = re.sub(r"<at[^>]*>.*?</at>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"^@\S+\s+", "", text.strip())
    return text.strip()


def parse_feishu_message(payload: Dict[str, Any]) -> Optional[FeishuMessage]:
    event = payload.get("event") or {}
    message = event.get("message") or {}
    if message.get("message_type") != "text":
        return None
    content_raw = message.get("content") or "{}"
    try:
        content = json.loads(content_raw)
    except json.JSONDecodeError:
        content = {}
    text = content.get("text", "")
    sender = event.get("sender", {}).get("sender_id", {})
    return FeishuMessage(
        chat_id=message.get("chat_id", ""),
        sender_open_id=sender.get("open_id", ""),
        text=text,
        message_id=message.get("message_id", ""),
    )


def parse_ws_message(data: Any) -> Optional[FeishuMessage]:
    payload = to_plain_dict(data)
    msg = parse_feishu_message(payload)
    if msg:
        return msg
    event = payload.get("event") or {}
    message = event.get("message") or {}
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    content_raw = message.get("content") or "{}"
    try:
        content = json.loads(content_raw)
    except json.JSONDecodeError:
        content = {}
    if message.get("message_type") != "text":
        return None
    return FeishuMessage(
        chat_id=message.get("chat_id", ""),
        sender_open_id=sender_id.get("open_id", ""),
        text=content.get("text", ""),
        message_id=message.get("message_id", ""),
    )


def to_plain_dict(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict):
        return data
    try:
        import lark_oapi as lark  # type: ignore

        marshaled = lark.JSON.marshal(data)
        if isinstance(marshaled, str):
            return json.loads(marshaled)
    except Exception:
        pass
    result: Dict[str, Any] = {}
    for name in ("schema", "header", "event"):
        if hasattr(data, name):
            value = getattr(data, name)
            result[name] = to_plain_dict(value) if not isinstance(value, (str, int, float, bool, type(None))) else value
    if result:
        return result
    for name in dir(data):
        if name.startswith("_"):
            continue
        try:
            value = getattr(data, name)
        except Exception:
            continue
        if callable(value):
            continue
        if isinstance(value, (str, int, float, bool, type(None), dict, list)):
            result[name] = value
    return result


class RequestHandler(http.server.BaseHTTPRequestHandler):
    router: CommandRouter
    config: Config
    seen_event_ids: Dict[str, float] = {}

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{utc_now()}] {self.address_string()} {fmt % args}", flush=True)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(200, {"ok": True, "time": utc_now()})
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/feishu/events":
            self.send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            self.send_json(400, {"error": "invalid json"})
            return

        if self._is_challenge(payload):
            if not self._verify_token(payload):
                self.send_json(401, {"error": "invalid verification token"})
                return
            self.send_json(200, {"challenge": payload.get("challenge", "")})
            return

        if not self._verify_signature(body):
            self.send_json(401, {"error": "invalid signature"})
            return
        if not self._verify_token(payload):
            self.send_json(401, {"error": "invalid verification token"})
            return
        if "encrypt" in payload:
            self.send_json(400, {"error": "encrypted event payload is not supported in v1"})
            return

        event_id = payload.get("header", {}).get("event_id", "")
        if event_id and self._seen(event_id):
            self.send_json(200, {"code": 0, "msg": "duplicated"})
            return
        msg = parse_feishu_message(payload)
        if msg:
            threading.Thread(target=self.router.handle_message, args=(msg,), daemon=True).start()
        self.send_json(200, {"code": 0})

    def _is_challenge(self, payload: Dict[str, Any]) -> bool:
        return payload.get("type") == "url_verification" or "challenge" in payload

    def _verify_token(self, payload: Dict[str, Any]) -> bool:
        expected = self.config.feishu.get("verification_token", "")
        if not expected:
            return True
        got = payload.get("header", {}).get("token") or payload.get("token") or ""
        return safe_compare(got, expected)

    def _verify_signature(self, body: bytes) -> bool:
        encrypt_key = self.config.feishu.get("encrypt_key", "")
        if not encrypt_key:
            return True
        timestamp = self.headers.get("X-Lark-Request-Timestamp", "")
        nonce = self.headers.get("X-Lark-Request-Nonce", "")
        signature = self.headers.get("X-Lark-Signature", "")
        if not timestamp or not nonce or not signature:
            return False
        digest = hashlib.sha256((timestamp + nonce + encrypt_key).encode("utf-8") + body).hexdigest()
        return safe_compare(digest, signature)

    def _seen(self, event_id: str) -> bool:
        now = time.time()
        expired = [k for k, t in self.seen_event_ids.items() if now - t > 3600]
        for k in expired:
            self.seen_event_ids.pop(k, None)
        if event_id in self.seen_event_ids:
            return True
        self.seen_event_ids[event_id] = now
        return False

    def send_json(self, code: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def build_server(config: Config) -> ThreadingHTTPServer:
    feishu = FeishuClient(config)
    tasks = TaskManager(config, feishu)
    router = CommandRouter(config, tasks, feishu)
    RequestHandler.config = config
    RequestHandler.router = router
    return ThreadingHTTPServer((config.host, config.port), RequestHandler)


def build_router(config: Config) -> CommandRouter:
    feishu = FeishuClient(config)
    tasks = TaskManager(config, feishu)
    return CommandRouter(config, tasks, feishu)


def run_http(config: Config) -> None:
    server = build_server(config)
    print(f"Codex 飞书控制服务已启动: http://{config.host}:{config.port}", flush=True)
    print(f"配置文件: {config.config_path}", flush=True)
    print(f"任务目录: {config.tasks_root}", flush=True)
    print(f"完成通知合并窗口: {config.finish_summary_window} 秒", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("收到退出信号，正在停止服务。", flush=True)
    finally:
        server.server_close()


def run_websocket(config: Config) -> None:
    app_id = config.feishu.get("app_id", "")
    app_secret = config.feishu.get("app_secret", "")
    if not app_id or not app_secret or app_secret.startswith("替换为"):
        raise RuntimeError("长连接模式需要配置 feishu.app_id 和 feishu.app_secret")
    try:
        import lark_oapi as lark  # type: ignore
    except ImportError as exc:
        raise RuntimeError("长连接模式需要安装官方 SDK：python3 -m pip install lark-oapi -U") from exc

    router = build_router(config)

    def on_message(data: Any) -> None:
        msg = parse_ws_message(data)
        if not msg:
            return
        threading.Thread(target=router.handle_message, args=(msg,), daemon=True).start()

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=event_handler,
        log_level=getattr(lark.LogLevel, "INFO", None),
        auto_reconnect=True,
    )
    print("Codex 飞书控制服务已启动：飞书长连接模式", flush=True)
    print(f"App ID: {app_id}", flush=True)
    print(f"配置文件: {config.config_path}", flush=True)
    print(f"任务目录: {config.tasks_root}", flush=True)
    print(f"完成通知合并窗口: {config.finish_summary_window} 秒", flush=True)
    client.start()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Codex 飞书控制服务")
    parser.add_argument("--config", required=True, help="配置文件路径，JSON 格式")
    args = parser.parse_args(argv)
    config = Config.from_file(args.config)
    if config.event_mode == "http":
        run_http(config)
    elif config.event_mode == "websocket":
        run_websocket(config)
    else:
        raise RuntimeError(f"未知 feishu.event_mode: {config.event_mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
