# codex-feishu-control

把本机 Codex 任务状态推送到飞书：任务完成、超时、卡住、需要人工介入时，通过飞书自建应用或 Webhook 给手机发送通知。

这个仓库包含两类能力：

- `hooks/notify_feishu.py`：Codex Hook / notify 事件的本地飞书通知脚本。
- `server.py`：可选的飞书控制守护进程，用飞书消息触发本机 `codex exec` 任务。

## 是否依赖大模型

飞书通知本身不依赖大模型。

部署后的 Hook、cron 进度巡检和飞书发送逻辑都在本机用 Python / Bash 运行，只依赖：

- 本机 Codex 产生 Hook / notify / 进程状态；
- Python 3；
- 飞书自建应用凭据或自定义机器人 Webhook；
- 能访问飞书开放平台 API 的网络。

只有当你启用 `server.py`，并通过飞书让它新建 Codex 任务时，任务执行部分才会调用 `codex exec`，也就是会用到 Codex 背后的模型。单纯的“任务完成推送、超时推送、进度巡检推送”不会调用大模型。

## 主要能力

- Codex 任务完成通知：处理 `Stop` / `turn-ended`，并对重复完成推送做短窗口去重。
- 需要确认通知：处理 `PermissionRequest`，这类重要事件不受普通完成通知静默窗口影响。
- 人工介入通知：当最终回复显示卡住、需要确认或无法继续时推送提醒。
- cron 进度巡检：`hooks/progress_check.sh` 可定时检查 `codex exec` 进程，有任务运行时推送设备、已运行时长和进程数。
- mini-monitor 兼容：保留 `CodexMonitorComplete`、`CodexMonitorTimeout`、`CodexMonitorStuck` 等事件识别。
- 安全存储：真实凭据只放本机配置文件，日志和示例文件不应包含 token、Webhook、App Secret 或真实 `open_id`。

## 快速部署 Hook 通知

```bash
git clone https://github.com/happy7blue/codex-feishu-control.git
cd codex-feishu-control

mkdir -p ~/.codex/hooks ~/.codex/logs
install -m 700 hooks/notify_feishu.py ~/.codex/hooks/notify_feishu.py
install -m 700 hooks/progress_check.sh ~/.codex/hooks/progress_check.sh
```

在本机创建 `~/.codex/hooks/feishu.env`，不要提交到仓库：

```bash
FEISHU_DELIVERY_MODE="app"
FEISHU_APP_ID="cli_xxxxxxxxxxxxxxxx"
FEISHU_APP_SECRET="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
FEISHU_RECEIVE_ID_TYPE="open_id"
FEISHU_RECEIVE_ID="ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
FEISHU_DEVICE_NAME="M4 MacBook Pro"
CODEX_NOTIFY_HOST_LABEL="M4 MacBook Pro"
```

然后设置权限：

```bash
chmod 600 ~/.codex/hooks/feishu.env
```

如果使用群聊自定义机器人 Webhook，可以只配置：

```bash
FEISHU_DELIVERY_MODE="webhook"
FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx"
```

两种方式选一种即可。

## 进度巡检 cron

`progress_check.sh` 默认设备名是 `Mini 2`，可用环境变量覆盖：

```bash
CODEX_PROGRESS_DEVICE_NAME="Mac mini" ~/.codex/hooks/progress_check.sh
```

注册 cron，每 30 分钟检查一次：

```bash
*/30 * * * * CODEX_PROGRESS_DEVICE_NAME="Mac mini" /Users/rex/.codex/hooks/progress_check.sh # codex-progress-check
```

没有 `codex exec` 进程时脚本静默退出；有进程时会通过 `notify_feishu.py` 推送进度。

## 可选：飞书控制守护进程

如果需要从飞书发消息来启动本机 Codex 任务，再配置 `server.py`：

```bash
cp config.example.json config.json
```

填写 `config.json` 中的飞书 App 凭据、项目白名单和 Codex 路径，并保持 `config.json` 只在本机保存。详细步骤见 [配置指南](references/setup-guide.md)，凭据验证见 [Token 验证指南](references/token-validation.md)。

## 敏感信息规则

不要提交这些文件或内容：

- `config.json`
- `hooks/feishu.env`
- `.env`
- `backups/`
- `*.log`
- `App Secret`、Webhook URL、tenant token、verification token、encrypt key、真实 `open_id` / `chat_id`

公开仓库只能保留占位示例，例如 `cli_xxxxxxxxxxxxxxxx`、`ou_xxxxxxxxxxxxxxxx`、`replace-with-a-long-random-token`。

提交前建议运行：

```bash
git status --short
git diff --cached --name-only
```

确认暂存区没有 `config.json`、`.env`、`feishu.env`、日志或备份文件。

## 许可证

本项目使用 MIT License，见 [LICENSE](LICENSE)。
