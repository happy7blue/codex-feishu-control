# Token 验证指南

配置完成后，用以下方法验证凭据是否有效，在启动服务前排除配置错误。

## 验证 App ID 和 App Secret
curl -s -X POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal \
  -H "Content-Type: application/json" \
  -d '{"app_id":"你的AppID","app_secret":"你的AppSecret"}'

正确响应 code 为 0，会返回 tenant_access_token。

常见错误：
- code 10003：App ID 或 App Secret 错误，回飞书开放平台重新复制
- code 99991663：应用未发布或未审批，检查应用发布状态
- code 99991400：请求格式错误，检查内容是否正确

## 验证机器人消息权限
curl -s -X POST "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id" \
  -H "Authorization: Bearer TENANT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"receive_id":"CHAT_ID","msg_type":"text","content":"{\"text\":\"验证消息：连接正常\"}"}'

常见错误：
- code 230002：没有发送消息权限，开放平台权限管理开启 im:message:send_as_bot
- code 230013：机器人不在会话中，将机器人添加到对应群聊或使用单聊

## 获取自己的 open_id
curl -s -X GET "https://open.feishu.cn/open-apis/contact/v3/users/me" \
  -H "Authorization: Bearer TENANT_TOKEN"

响应中 data.user.open_id 即为你的 open_id，格式为 ou_xxxxxxxx，填入 config.json 的 allowed_open_ids。

## dry_run 验证
1. config.json 中 dry_run 设为 true
2. 启动服务
3. 在飞书发送"帮助"
4. 查看终端输出，确认收到消息并打印了将要发送的回复
5. 确认无误后将 dry_run 改为 false，重启服务
