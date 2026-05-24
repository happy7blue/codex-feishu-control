# Changelog

## 2026-05-24

### Fixed

- 补回 `codex-mini-monitor` 依赖的 `load_monitor_state`、`save_monitor_state`、`update_monitor_state` 等兼容函数。
- 恢复 `CodexMonitorComplete`、`CodexMonitorTimeout`、`CodexMonitorStuck` 三类验收事件的飞书推送。
- 修复 Mac mini 环境中 shell 变量覆盖 `feishu.env` 后造成的 `open_id cross app` 风险：飞书 App 凭证和接收人 ID 优先从 `feishu.env` 读取，保证 App ID 与 open_id 成对使用。

### Notes

- Mac mini 本地应继续将 `feishu.env` 保存在 `/Users/rex/.codex/hooks/feishu.env`，并保持 `600` 权限。
- 验收覆盖完成、超时和卡住三种通知，均以飞书接口返回 `code: 0`、日志 `sent: true` 为准。
