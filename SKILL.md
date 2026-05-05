---
name: codex-feishu-control
description: |
  Codex 任务监控与人工接管通知。监控本机正在运行的 Codex 任务，当任务完成、失败、超时或陷入死循环时，主动通过飞书推送告警。让你放心离开电脑，看到消息再回来处理。具备风险管控、进展去重、防死循环强制终止等能力。
  触发关键词：codex 监控、任务告警、人工接管、死循环告警、飞书通知、任务完成推送、超时告警、setup、start、stop、status、logs、doctor。
  不适用于：构建独立飞书机器人、webhook 集成、或与飞书 SDK 相关的普通编程任务。
argument-hint: "setup | start | stop | status | logs [N] | reconfigure | doctor"
allowed-tools: Bash, Read, Write, Glob
---

# Codex 飞书控制 Skill

后台守护进程将飞书消息桥接到本机 Codex CLI，任务完成、超时、或陷入死循环时主动推送飞书通知。

架构：你（飞书手机端）→ 飞书 Bot API → 后台守护进程（Python）→ codex exec → 读写本机代码库

## 子命令

收到用户指令后，先解析意图，映射到以下子命令之一：

- setup：引导填写飞书凭据和项目路径，触发示例"帮我配置"、"初始化"
- start：启动后台守护进程，触发示例"启动服务"、"跑起来"
- stop：停止守护进程，触发示例"停止服务"、"关掉"
- status：查看服务和当前任务是否正常运行，触发示例"现在状态怎么样"、"在跑吗"
- logs [N]：查看任务日志默认50行，触发示例"看日志"、"最近50条"
- reconfigure：修改已有配置，触发示例"改配置"、"换项目"
- doctor：诊断服务异常，触发示例"没反应了"、"挂了"、"出问题了"

消歧原则：status 用于主动询问状态；doctor 用于用户描述症状或报告异常。收到"没反应"、"挂了"等描述时，优先用 doctor。

## 核心能力

人工接管通知（核心价值）：
- 进展通知：任务运行超过阈值（默认30分钟）且输出有实质变化时推送摘要，内容无变化自动跳过避免重复打扰
- 完成通知：任务成功、失败、超时、被停止时推送结果摘要
- 完成通知合并：60秒窗口内多个任务完成合并为一条推送，单条任务显示详细摘要，多条显示批量汇总
- 任务冲突检测：新任务启动前 AI 自动判断与当前运行任务是否冲突，冲突则进入队列等待，无冲突直接并行执行，结果实时推送告知
- 死循环防护：timeout_seconds（默认1800秒）到达后强制终止并告警

风险管控：
- 硬拒绝：读取密钥、token、.env、full-auto 模式等
- 高风险降级：rm -rf、git push、sudo 等自动进入只读计划模式
- 项目白名单：只有配置了路径的项目才能执行任务

安全存储：凭据权限 600 存储，所有日志自动脱敏，token 不会出现在任务输出中。

## 推送消息格式

进展通知：
【进展汇总】<project_alias>
已运行：约 N 分钟
通知次数：第 N 次
状态变化：输出有新增内容 / 输出无变化，任务仍在运行
最近输出（节选）：<output.log 尾部最多300字符>

完成通知：
任务完成 / 任务失败 / 任务超时 / 任务已停止
任务: <task_id>
项目: <project_alias>
状态: <status>
摘要：<last_message.txt 或 output.log 尾部>

## 关键配置参数

codex 块：
- timeout_seconds 默认1800：任务最长运行秒数，超时强制终止并告警
- progress_interval_seconds 默认1800：进展通知间隔秒数，0表示不推进展
- progress_summary_window 默认0：时间窗口内多次触发合并为一次推送，0禁用
- notify_on_start 默认false：是否在任务启动时立即推送
- sandbox 默认workspace-write：低风险任务沙箱模式
- model 默认空：指定 Codex 模型，空则不传

feishu 块：
- event_mode 默认websocket：websocket 或 http
- app_id 默认空：飞书自建应用 App ID
- app_secret 默认空：飞书自建应用 App Secret
- allowed_open_ids 默认空数组：用户白名单，空则不限制
- dry_run 默认true：true 时只打印不实际发送，调试用

## 快速部署
git clone https://github.com/yourname/codex-feishu-control.git
cd codex-feishu-control
cp config.example.json config.json
填写 feishu.app_id、app_secret 和 projects 路径，将 dry_run 改为 false
cp launch_agents/com.swq.codex-feishu-control.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.swq.codex-feishu-control.plist
在飞书发送"帮助"验证连通

详细配置步骤见 references/setup-guide.md，token 获取方式见 references/token-validation.md。

## 边界说明
- 不支持交互式 approve/reject，高风险任务只能进入只读计划或拒绝
- 不支持 pty，Codex 通过非交互 codex exec 运行
- 飞书发送失败不阻塞任务完成，异常写入任务日志
