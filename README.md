# codex-feishu-control

Codex 飞书通知与 mini-monitor 兼容脚本。当前仓库重点维护 `hooks/notify_feishu.py`，用于把 Codex hook 和 `codex-mini-monitor` 的完成、超时、卡住事件发送到飞书自建应用。

## 本次修复

- 补回 `codex_task_watchdog.py` 依赖的 mini-monitor 兼容层，包括 `CodexMonitorComplete`、`CodexMonitorTimeout`、`CodexMonitorStuck` 事件识别、状态文件读写和监控消息格式化。
- `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_RECEIVE_ID_TYPE`、`FEISHU_RECEIVE_ID` 改为优先读取 `hooks/feishu.env`，避免 shell 环境里残留的旧 App ID 覆盖本地配置，导致 `open_id cross app`。
- 保留普通 Codex `Stop` / `PermissionRequest` 通知逻辑，并继续忽略 `PostToolUse` 通知，减少误报。

## Mac mini 兼容说明

Mac mini 上推荐把脚本安装到：

```bash
/Users/rex/.codex/hooks/notify_feishu.py
/Users/rex/.codex/hooks/feishu.env
/Users/rex/.codex/bin/codex-mini-monitor
```

`feishu.env` 必须只保存在本机，不要提交到仓库。建议权限为 `600`，并确保同一个飞书 App 下查询得到的 `open_id` 写入 `FEISHU_RECEIVE_ID`。

示例配置：

```bash
FEISHU_DELIVERY_MODE="app"
FEISHU_APP_ID="cli_xxxxxxxxxxxxxxxx"
FEISHU_APP_SECRET="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
FEISHU_RECEIVE_ID_TYPE="open_id"
FEISHU_RECEIVE_ID="ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
FEISHU_WEBHOOK_URL=""
```

如果 `.zshrc` 或其他 shell 启动文件中也设置了 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，请让它们来源于同一个 `feishu.env`，避免 App ID 与 open_id 不匹配。

## 验收命令

```bash
/Users/rex/.codex/bin/codex-mini-monitor simulate complete
/Users/rex/.codex/bin/codex-mini-monitor simulate timeout --duration-seconds 3665
/Users/rex/.codex/bin/codex-mini-monitor simulate stuck --step "等待用户确认权限"
```

预期输出中的 `ok` 为 `true`，并且 `/Users/rex/.codex/logs/notify_feishu.log` 中出现 `monitor watchdog notification processed` 且 `sent: true`。

## 安全提示

- 不要提交 `hooks/feishu.env`、备份文件、token、App Secret 或真实 webhook URL。
- `open_id` 与 App 绑定。如果飞书返回 `open_id cross app`，请用当前 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 的 tenant token 重新查询接收人的 `open_id`。
