# P1 个人账号桥接（Issue #3）

客户添加原有个人账号，而非机器人账号。P0 的模拟 Bot 运行时保留；P1 是独立增量，不把 P0 证据当作真实接入证据。当前代码仅提供 Telegram connected-account 契约及待执行测试，尚未接入网络、现有 Service、飞书或真实模型。

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
| P1-B | todo | 实际 getUpdates 长轮询、持久游标、连接权限更新及断线恢复；在进入 Service 前过滤，owner 消息暂停 AI 并镜像而不是进入 customer ingest；出站使用持久连接和消息回执，不使用普通 Bot send/delete |
| P1-C | todo | 飞书长连接，真实员工收发；M6 翻译、DGX AI 和物流样例接入 |
| P1-D | blocked | WhatsApp 原号码合法接入方式确认及测试 |
| P1-E | blocked | MAX 平台明确允许该桥接场景 |
| P1-F | todo | Linux 行为及回归证据、两名测试身份真实端到端、重启/撤回/隐私隔离 |

禁止记录白名单外原始消息，禁止把 owner 事件当作 customer 事件。连接和白名单必须在入站、出站及重试时校验；当前契约状态只在内存中，生产 runner 必须持久化有效状态、游标、消息映射，并验证授权撤销和错序。未获真实回执不得宣称撤回成功。未经测试账号授权不发送真实消息。

复验命令：PYTHONPATH=src python3 -m unittest discover -s tests -p test_personal.py -v；随后 bash scripts/acceptance.sh。当前提交尚未运行，不能标 PASS。
