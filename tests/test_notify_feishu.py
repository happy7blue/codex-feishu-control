#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
NOTIFY_SCRIPT = REPO_ROOT / "hooks" / "notify_feishu.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NotifyFeishuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.codex_home = Path(cls.tempdir.name) / ".codex"
        (cls.codex_home / "hooks").mkdir(parents=True)
        (cls.codex_home / "logs").mkdir(parents=True)
        os.environ["CODEX_HOME"] = str(cls.codex_home)
        cls.notify = load_module("notify_feishu_under_test", NOTIFY_SCRIPT)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tempdir.cleanup()
        os.environ.pop("CODEX_HOME", None)

    def setUp(self) -> None:
        for path in (
            self.notify.STATE_FILE,
            self.notify.STATE_FILE.with_suffix(".lock"),
        ):
            path.unlink(missing_ok=True)

    def transcript(self, name: str, rows: list[dict]) -> Path:
        path = self.codex_home / "sessions" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        return path

    def permission_data(self, transcript: Path, reviewer: str) -> dict:
        turn_id = "turn-permission"
        transcript.write_text(
            json.dumps(
                {
                    "timestamp": "2026-07-26T00:00:00Z",
                    "type": "turn_context",
                    "payload": {
                        "turn_id": turn_id,
                        "approval_policy": "on-request",
                        "approvals_reviewer": reviewer,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "hook_event_name": "PermissionRequest",
            "session_id": "session-permission",
            "turn_id": turn_id,
            "transcript_path": str(transcript),
            "cwd": "/workspace",
            "tool_name": "Bash",
            "tool_input": {"command": "true"},
        }

    def test_auto_review_permission_is_suppressed(self) -> None:
        transcript = self.transcript("auto-review", [])
        data = self.permission_data(transcript, "auto_review")
        skip, reason = self.notify.should_skip_notification(
            data,
            "Codex 需要确认",
            "需要权限确认",
            {},
        )
        self.assertTrue(skip)
        self.assertIn("auto_review", reason)

    def test_user_permission_is_immediate_then_rate_limited(self) -> None:
        transcript = self.transcript("user-review", [])
        data = self.permission_data(transcript, "user")
        first_skip, _ = self.notify.should_skip_notification(
            data,
            "Codex 需要确认",
            "需要权限确认",
            {},
        )
        second_skip, reason = self.notify.should_skip_notification(
            data,
            "Codex 需要确认",
            "需要权限确认",
            {},
        )
        self.assertFalse(first_skip)
        self.assertTrue(second_skip)
        self.assertIn("300s", reason)

    def test_stop_requires_task_complete_for_same_turn(self) -> None:
        current_turn = "turn-current"
        transcript = self.transcript(
            "completion",
            [
                {
                    "timestamp": "2026-07-26T00:00:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-other",
                        "completed_at": 1785024001,
                        "last_agent_message": "其他任务已完成",
                    },
                },
                {
                    "timestamp": "2026-07-26T00:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": "当前任务的过程消息",
                    },
                },
            ],
        )
        data = {
            "hook_event_name": "Stop",
            "turn_id": current_turn,
            "transcript_path": str(transcript),
            "last_assistant_message": "不能作为完成证据",
        }
        self.assertEqual(self.notify.completion_evidence_message(data, 1785024000), "")

        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": "2026-07-26T00:00:03Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": current_turn,
                            "completed_at": 1785024003,
                            "last_agent_message": "当前任务确实完成",
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        self.assertEqual(
            self.notify.completion_evidence_message(data, 1785024000),
            "当前任务确实完成",
        )

    def test_last_assistant_message_is_not_completion_evidence(self) -> None:
        data = {
            "hook_event_name": "Stop",
            "turn_id": "turn-without-transcript",
            "last_assistant_message": "这只是过程消息",
        }
        self.assertEqual(self.notify.completion_evidence_message(data, 1785024000), "")


if __name__ == "__main__":
    unittest.main()
