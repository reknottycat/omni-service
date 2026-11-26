# P1 个人账号桥接（Issue #3）

客户添加原有个人账号，而非机器人账号。P0 的模拟 Bot 运行时保留；P1 是独立增量，不把 P0 证据当作真实接入证据。已有独立的单会话测试入口，复用 Service 的持久收件和发件队列，通过 Telegram Bot API 和本机飞书 CLI 连接两端。真实双向联调等待平台绑定和成员核验。

## 用户准备

- Telegram：手机/桌面客户端已登录；在 https://t.me/BotFather 创建专用测试 Bot，开启 Secretary Mode；在个人账号设置中连接它（菜单名称因版本而异）。只允许指定测试联系人，允许回复与删除机器人自己发送的消息；不要授予删除所有消息、修改个人资料、资金等权限。Bot Token 仅在部署主机密钥配置中保存。确认自己的数字用户 ID、授权测试联系人（其 chat ID 可在受限联调时获得）。不要提供个人账号密码、验证码或 session 文件。官方：https://core.telegram.org/bots/features#business-bots 及 https://core.telegram.org/api/bots/connected-business-bots 。回复受最近 24 小时收到消息的限制。
- 飞书：可先创建自己的测试组织/工作区，管理员创建企业自建应用，开启机器人、长连接事件及群聊消息读取/发送/建群/成员权限。提供 App ID（非密钥）、管理员及测试员工 Open ID；App Secret 本机保存。无需公网。官方：https://open.feishu.cn/
- WhatsApp：现有号码及手机可正常登录即可；先评估官方 Business 原号码方案，官方 Cloud API 的接收通常需要 HTTPS Webhook，暂缓；非官方关联设备有封号及协议风险，不自动启用。无需提供 Facebook 密码或短信验证码。
- MAX：现有账号登录不等于接口授权。官方 Open Client API 明确限制服务器桥接和向第三方基础设施转移数据，当前个人桥接标为 blocked，等待平台明确允许的路径。https://legal.max.ru/openclient-docs
- M6 / DGX Spark：准备各自模型服务的局域网/Tailscale 地址、端口、API 类型、模型 ID（如需认证则密钥本机保存）。先不配置公网。中俄物流知识库用合成样例，不作为真实报价。

## 工作追踪及验收

| ID | 状态 | 完成条件 |
|---|---|---|
| P1-A | verify | Telegram 连接、授权私聊白名单、身份/回环分类、代发和撤回请求构造；运行 tests/test_personal.py 并保留证据 |
| P1-B | verify | 独立入口已实现长轮询、持久游标、会话过滤、本人回复镜像和带连接 ID 的原账号代发。待真实私聊双向验收与撤回接入 |
| P1-C | verify | 飞书机器人身份和长连接握手成功；私有测试群已创建，成员表读取权限及普通群消息送达待验。当前使用固定译文，M6 翻译和 DGX AI 待接入 |
| P1-D | blocked | WhatsApp 原号码合法接入方式确认及测试 |
| P1-E | blocked | MAX 平台明确允许该桥接场景 |
| P1-F | todo | Linux 行为及回归证据、两名测试身份真实端到端、重启/撤回/隐私隔离 |

禁止记录白名单外原始消息，禁止把 owner 事件当作 customer 事件。连接和白名单必须在入站、出站及重试时校验；测试入口持久化游标、绑定、消息和回执，并从 Telegram 刷新当前授权后投递。未获真实回执不得宣称撤回成功。未经测试账号授权不发送真实消息。

复验命令：PYTHONPATH=src python3 -m unittest discover -s tests -p test_personal.py -v；随后 bash scripts/acceptance.sh。P1 真实账号联调仍未运行。

## 白名单迭代器修复（2026-11-26）

任务块：修复一次性迭代器作为白名单时被校验消耗的问题，避免已授权联系人被误拒绝；修改 `personal.py` 和对应行为测试，不改变默认拒绝策略。依赖现有 P1 契约；复验为 `PYTHONPATH=src python3.12 -m unittest discover -s tests -p test_personal.py -v`，通过条件是授权联系人可解析、无效 ID 被拒绝。

本机 macOS / Python 3.12 上行为测试通过；完整测试 95 项中千客户 HTTP 测试发生连接重置，原因尚未确认。2026-11-26 在 DGX Linux aarch64、Python 3.12.3 上以 e70497a 加本地白名单修复工作树运行 `bash scripts/acceptance.sh`，95 项通过、0 失败、0 跳过，源码 SHA256 `405f0b177bf6ee4954ace58ab78d54586183eaac0a96cd0fd2ea9651ea976220`；本地原始报告及日志为 `artifacts/dgx-20261126/acceptance.json` 和 `acceptance.log`。该结果包含契约测试与 P0 模拟，不代表 P1 实际联调。

## 单会话文字联调入口

这次联调复用持久队列，限定一个有明确授权的 Telegram 测试联系人、一个飞书私有群和指定员工。配置字段以 `config/personal.example.json` 为准。真实测试需在 Telegram 客户端将专用 Secretary Bot 只连接到该测试联系人，飞书应用启用消息长连接并允许读取机器人所加入群的成员名单。配置数据与运行库放在本机忽略目录中。机器人凭据通过环境变量或 `--ask-token` 的隐藏输入加载。

```bash
PYTHONPATH=src python3.12 -m omni_service.personal_runner discover --config artifacts/personal-pilot/local.json --ask-token
PYTHONPATH=src python3.12 -m omni_service.personal_runner check --config artifacts/personal-pilot/local.json --ask-token
PYTHONPATH=src python3.12 -m omni_service.personal_runner run --config artifacts/personal-pilot/local.json --db artifacts/personal-pilot/bridge.sqlite3 --ask-token
```

`discover` 只报告 Telegram 连接状态和绑定标识。填写 `connection_id`、`expected_owner_id` 和唯一的 `allowed_chat_ids` 后才可运行桥接。服务端以 Telegram 返回的当前权限为准。飞书预检按实际成员列表校验指定员工和应用，群跨组织、成员不匹配或缺少相应权限都会停止。

测试联系人在 Telegram 向原个人账号发送 `Здравствуйте`，飞书员工应在测试群看到原文和 `你好`。员工发送 `您好，我来帮您处理。`，Telegram 联系人应收到俄译，回执包含预期私聊 ID 和商用连接 ID。重启后重放同一消息，接收方应只看到一次。此入口仅处理普通文字并显式使用固定译文，不启用 AI、媒体、编辑或撤回。

`2026-11-26` 真实联调阻塞由协调负责人跟进。Telegram 专用测试机器人已创建且开启 Secretary Mode，等待选定测试联系人并绑定。飞书测试群已创建，机器人身份及消息长连接握手正常，`im +chat-members-list --as bot` 返回 `99991672`，待应用取得 `im:chat.members:read` 并验证普通群消息。上述平台操作未发送真实客户消息。
