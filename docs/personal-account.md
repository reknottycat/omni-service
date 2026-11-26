# P1 个人账号桥接（Issue #3）

客户添加原有个人账号，而非机器人账号。P0 的模拟 Bot 运行时保留；P1 是独立增量，不把 P0 证据当作真实接入证据。已有独立的单会话测试入口，复用 Service 的持久收件和发件队列，通过 Telegram Bot API 和本机飞书 CLI 连接两端。真实双向联调等待平台绑定和成员核验。

## 用户准备

- Telegram：手机/桌面客户端已登录；在 https://t.me/BotFather 创建专用测试 Bot，开启 Secretary Mode；在原个人账号的「设置 → 聊天自动化」连接它。Telegram 官方确认此入口对普通账号开放，无需 Premium。旧版客户端可能在「Telegram 企业版」显示相关入口。只允许指定测试联系人，允许回复与删除机器人自己发送的消息；不要授予删除所有消息、修改个人资料、资金等权限。Bot Token 仅在部署主机密钥配置中保存。确认自己的数字用户 ID、授权测试联系人（其 chat ID 可在受限联调时获得）。不要提供个人账号密码、验证码或 session 文件。官方：https://core.telegram.org/bots/features#business-bots 及 https://core.telegram.org/api/bots/connected-business-bots 。回复受最近 24 小时收到消息的限制。
- 飞书：可先创建自己的测试组织/工作区，管理员创建企业自建应用，开启机器人、长连接事件及群聊消息读取/发送/建群/成员权限。提供 App ID（非密钥）、管理员及测试员工 Open ID；App Secret 本机保存。无需公网。官方：https://open.feishu.cn/
- WhatsApp：现有号码及手机可正常登录即可；先评估官方 Business 原号码方案，官方 Cloud API 的接收通常需要 HTTPS Webhook，暂缓；非官方关联设备有封号及协议风险，不自动启用。无需提供 Facebook 密码或短信验证码。
- MAX：现有账号登录不等于接口授权。官方 Open Client API 明确限制服务器桥接和向第三方基础设施转移数据，当前个人桥接标为 blocked，等待平台明确允许的路径。https://legal.max.ru/openclient-docs
- M6 / DGX Spark：准备各自模型服务的局域网/Tailscale 地址、端口、API 类型、模型 ID（如需认证则密钥本机保存）。先不配置公网。中俄物流知识库用合成样例，不作为真实报价。

## 工作追踪及验收

| ID | 状态 | 完成条件 |
|---|---|---|
| P1-A | done | Telegram 契约测试已在 DGX 通过，含默认拒绝白名单、本人及回环分类、代发和撤回请求构造 |
| P1-B | verify | 独立入口已实现长轮询、持久游标、会话过滤、本人回复镜像和带连接 ID 的原账号代发。待真实私聊双向验收与撤回接入 |
| P1-C | verify | 飞书机器人身份、长连接握手和私有测试群成员核验已通过，普通群消息送达待验。DGX 中俄实际译文和飞书普通消息收发已验证，跨平台联调与自动接待待验 |
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

测试联系人正常给原 Telegram 账号发俄文，飞书员工应在测试群看到原文和中文意思。员工在群内按普通方式回复中文，测试联系人应从原账号收到俄文，回执包含预期私聊 ID 和连接 ID。重启后重放同一消息，接收方应只看到一次。真实联调用 `translation.mode=private-model`，固定译文模式仅用于合成回归。此入口当前处理普通文字，自动接待、媒体、编辑和撤回分别验收。

2026-11-26 在 DGX Linux aarch64 / Python 3.12.3 上从提交 `c5aeed5` 运行 `bash scripts/acceptance.sh`，111 项全部通过，0 失败、0 跳过，源码 SHA256 为 `923d6e7e3107ca6767986a12fd264b713345bc88bad30c330f17572c9a60229b`。原始结果在本机 `artifacts/dgx-pilot-20261126/acceptance.json` 和 `acceptance.log`。包括双向固定译文、重放、权限撤销、错误回执处理的合成用例。

真实联调由协调负责人跟进。Telegram Bot API 已核实专用机器人身份、Secretary Mode 和无 Webhook。专用机器人收到测试账号的 `/start`，数字身份已写入本机唯一白名单。连接发现仍返回空数组，待 Telegram 主账号仅授权该私聊并取得连接 ID。测试账号 ID 及凭据保留在本机配置中。

飞书应用起初以 `99991672` 拒绝群成员查询，获得用户确认后开通 `im:chat.members:read`。实际预检已确认群为内部私有群，名单与配置一致，长连接握手通过。获得用户确认后已开通 `im:message.group_msg`，并实际收到测试群内不带 @ 的人员文字。单独通过桥接发送器把 DGX 俄译中样本发到测试群，取得飞书消息回执。


DGX 私有模型 `qwen3-vl:4b-instruct` 已经通过桥接翻译入口实测两条自然物流样本。核对了哈巴罗夫斯克、同江、18 吨玉米、12 月 5 日、人民币 8600 元及不含报关费。原始输入、译文及飞书投递回执保存在本机 `artifacts/personal-pilot/translation-check.json`。Telegram 端仍以看到原账号真实发出回执和 Sandra 收到消息为验收条件。

加入实际翻译后的源码提交 `e8d1e72` 在同一 DGX 环境通过完整模拟验收，115 项通过、0 失败、0 跳过。源码 SHA256 为 `23df07c551c1920ebc35e02320027c5c51dea0963cad5cacaa2aa76f33a438c5`，报告与日志在本机 `artifacts/dgx-translation-20261126/`。

## 普通账号找不到连接入口时

首选 [Telegram Web K](https://web.telegram.org/k/)。其公开源码在[编辑个人资料](https://github.com/morethanwords/tweb/blob/master/src/components/sidebarLeft/tabs/editProfile.tsx#L422-L472)提供独立的 Chat Automation，也可从[设置搜索](https://github.com/morethanwords/tweb/blob/master/src/lib/settingsSearch/openers.ts#L118-L125)打开。连接时选择 Only Selected、指定测试联系人、开启 Reply。当前 Web K 显示扫码登录，登录后的页面和连接回执还未实测。

备选官方 [Telegram Desktop 7.4.6](https://github.com/telegramdesktop/tdesktop/releases/tag/v7.4.6)。[该版本源码](https://github.com/telegramdesktop/tdesktop/blob/v7.4.6/Telegram/SourceFiles/settings/sections/settings_business.cpp#L506-L516)对免费用户开放 Telegram Business 菜单下的 Chatbots，和本机 App Store 原生 Telegram 的菜单实现不同。在两者都不可用时，[TDLib](https://core.telegram.org/tdlib/docs/classtd_1_1td__api_1_1set_business_connected_bot.html)可以通过由本人授权的临时用户会话完成一次账号绑定；该方案尚未在本机测试。

[Vercel Chat SDK 的 Telegram 适配器](https://github.com/vercel/chat/blob/e490e84f01dbf477a5d1a9e7eec16c3fe72a31ac/packages/adapter-telegram/src/index.ts#L1172-L1225)按连接 ID 和聊天 ID 隔离会话并过滤机器人回环。[Chatwoot 开发分支](https://github.com/chatwoot/chatwoot/blob/38258d313d147657be00acb4a5d7f6060422e34d/app/services/telegram/incoming_message_service.rb#L105-L118)持久化连接 ID，[回复时将其放入请求](https://github.com/chatwoot/chatwoot/blob/38258d313d147657be00acb4a5d7f6060422e34d/app/models/channel/telegram.rb#L157-L169)。OmniService 采用同一 Bot API 协议，入站用本机长轮询，无需公网 Webhook。当前缺口是原个人账号完成首次授权。

## DGX Linux 运行

Web K 或桌面客户端只做一次 Telegram 账号授权。DGX 上同一个桥接进程负责 Telegram 长轮询、飞书长连接及双向消息队列；翻译使用 DGX 的 `http://127.0.0.1:11434/v1`。飞书在 Linux 上配置应用身份，按测试群名单预检。`lark-cli` 1.0.99 官方 Linux arm64 二进制已在隔离目录核对 SHA256、版本和消息事件 schema；Linux 应用凭据尚未配置。

DGX 直连 Telegram 超时，飞书 HTTPS 和本机翻译已通。使用 DGX 已配置的美国 SSH 主机，以 `ssh -N -T -o ExitOnForwardFailure=yes -L 127.0.0.1:37892:api.telegram.org:443 <美国主机别名>` 建立转发，再给桥接进程设置 `TELEGRAM_TLS_TUNNEL_PORT=37892`。这是 Telegram 专用转发，程序仍校验 Telegram 的 TLS 证书。若部署改用私网 HTTP CONNECT 代理，可单独配置 `TELEGRAM_HTTPS_PROXY`，两个配置互斥。

已实测从 DGX 经美国主机调用 `getMe` 和 `getWebhookInfo` 成功，且确认 Secretary Mode 开启。测试结束已关闭临时转发。待原账号授权和 Linux 飞书凭据配置后，先验证真实文字双向收发，再将隧道和桥接进程设为长期运行并复测重启。整个链路只运行一个 Telegram 长轮询实例。

包含美国 SSH 转发支持的提交 `cdb3bfc` 在 DGX 完整模拟验收通过 118 项，源码指纹 `58d0416af863da4c277945077cd2e7ef68148613de75d9b0fc235beb63179a87`。原始报告与日志见本机 `artifacts/dgx-us-egress-20261126/`。
