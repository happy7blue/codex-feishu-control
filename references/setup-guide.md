# 配置指南

## 第一步：创建飞书自建应用
1. 打开 https://open.feishu.cn/app
2. 进入开发者后台
3. 点击创建企业自建应用，名称随意，比如"Codex 控制员"
4. 左侧点击凭证与基础信息
5. 在应用凭证区域复制 App ID 和 App Secret

注意：App Secret 相当于密码，只放在本地 config.json，不要提交到 Git 仓库，不要发给任何人。

## 第二步：开启机器人能力
1. 左侧菜单点击添加应用能力
2. 找到机器人，点击开启
3. 左侧点击权限管理，搜索并开启以下权限：
   - im:message（接收消息）
   - im:message:send_as_bot（发送消息）

## 第三步：配置事件订阅（WebSocket 模式，推荐）
1. 左侧点击事件与回调
2. 选择长连接模式（WebSocket）
3. 添加事件：im.message.receive_v1（接收消息事件）

## 第四步：发布应用
1. 左侧点击版本管理与发布
2. 创建版本，填写描述，提交发布
3. 企业管理员审批后生效（自测可用测试版本）

## 第五步：配置 config.json
复制 config.example.json 为 config.json，填写以下字段：

{
  "feishu": {
    "app_id": "cli_xxxxxxxxxxxxxx",
    "app_secret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "event_mode": "websocket",
    "allowed_open_ids": [],
    "dry_run": false
  },
  "projects": {
    "demo": "/Users/yourname/projects/demo"
  },
  "codex": {
    "timeout_seconds": 1800,
    "progress_interval_seconds": 1800
  }
}

allowed_open_ids 留空表示不限制用户；填入你的 open_id 表示只有你能用（推荐）
dry_run 调试时设为 true，确认正常后改为 false

## 第六步：安装并启动服务
cp launch_agents/com.swq.codex-feishu-control.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.swq.codex-feishu-control.plist
launchctl list | grep codex-feishu

在飞书给机器人发送"帮助"，收到回复即表示配置成功。

## HTTP 模式补充
如果使用 HTTP 模式，event_mode 改为 http，飞书事件订阅页面填入回调地址 https://your-domain.com/feishu/events，并将 verification_token 填入 config.json。
