# P1 个人账号桥接（Issue #3）

客户正常私聊原有 Telegram 个人账号。P1 单会话测试入口复用持久收件和发件队列，通过 Telegram Bot API 和飞书 CLI 连接两端。Mac 已完成双向真实文字验收，DGX 已完成同版本的 Linux 模拟回归。

## 用户准备

- Telegram 在原账号的「设置 → 聊天自动化」连接已开启 Secretary Mode 的专用机器人。普通账号可以使用，无需 Premium。文字试点只授权测试会话的读取与回复。Bot Token 在运行主机的密钥配置中保存。回复受最近 24 小时收到消息的限制。[官方说明](https://core.telegram.org/bots/features#business-bots) · [连接协议](https://core.telegram.org/api/bots/connected-business-bots)
- 飞书使用已有测试组织的自建应用。试点需要机器人发消息、接收普通群消息的长连接事件，以及读取测试群成员名单的权限。App Secret 保存在运行主机的密钥配置中。[飞书开放平台](https://open.feishu.cn/)
- WhatsApp：现有号码及手机可正常登录即可；先评估官方 Business 原号码方案，官方 Cloud API 的接收通常需要 HTTPS Webhook，暂缓；非官方关联设备有封号及协议风险，不自动启用。无需提供 Facebook 密码或短信验证码。
- MAX：现有账号登录不等于接口授权。官方 Open Client API 明确限制服务器桥接和向第三方基础设施转移数据，当前个人桥接标为 blocked，等待平台明确允许的路径。https://legal.max.ru/openclient-docs
- M6 / DGX Spark：准备各自模型服务的局域网/Tailscale 地址、端口、API 类型、模型 ID（如需认证则密钥本机保存）。先不配置公网。中俄物流知识库用合成样例，不作为真实报价。

## 工作追踪及验收

| ID | 状态 | 完成条件 |
|---|---|---|
| P1-A | done | Telegram 契约测试已在 DGX 通过，含默认拒绝白名单、本人及回环分类、代发和撤回请求构造 |
| P1-B | verify | 原账号真实私聊双向文字已在 Mac 验收，待真实撤回接入 |
| P1-C | done | 私有测试群的普通消息、客户显示名、DGX 中俄互译、AI 接待转人工和群内提及隔离已通过真实联调 |
| P1-D | blocked | WhatsApp 原号码合法接入方式确认及测试 |
| P1-E | blocked | MAX 平台明确允许该桥接场景 |
| P1-F | verify | 118 项 Linux 模拟回归通过，两名测试账号在 Mac 真实收发通过，复用原数据库重启后继续收发。Linux 真实联调和真实撤回待验 |

禁止记录白名单外原始消息，禁止把 owner 事件当作 customer 事件。连接和白名单必须在入站、出站及重试时校验；测试入口持久化游标、绑定、消息和回执，并从 Telegram 刷新当前授权后投递。未获真实回执不得宣称撤回成功。未经测试账号授权不发送真实消息。

契约复验用 `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_personal*.py' -v`，完整模拟复验用 `bash scripts/acceptance.sh`。

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

Telegram 主账号已在 Web K 为专用机器人授权指定测试私聊的读取和回复。Bot API 核实了账号、连接 ID、启用状态和回复权限。测试账号 ID 及凭据保留在本机配置中。

飞书应用起初以 `99991672` 拒绝群成员查询，获得用户确认后开通 `im:chat.members:read`。实际预检已确认群为内部私有群，名单与配置一致，长连接握手通过。获得用户确认后已开通 `im:message.group_msg`，并实际收到测试群内不带 @ 的人员文字。单独通过桥接发送器把 DGX 俄译中样本发到测试群，取得飞书消息回执。


DGX 私有模型 `qwen3-vl:4b-instruct` 已通过桥接入口实测中俄物流样本。核对了哈巴罗夫斯克、同江、18 吨玉米、12 月 5 日、人民币 8600 元及不含报关费。单独翻译检查见本机 `artifacts/personal-pilot/translation-check.json`。

加入实际翻译后的源码提交 `e8d1e72` 在同一 DGX 环境通过完整模拟验收，115 项通过、0 失败、0 跳过。源码 SHA256 为 `23df07c551c1920ebc35e02320027c5c51dea0963cad5cacaa2aa76f33a438c5`，报告与日志在本机 `artifacts/dgx-translation-20261126/`。

### 2026-11-26 Mac 真实验收

Mac arm64 / Python 3.12.16 通过上面的运行入口，使用真实 Telegram、飞书和 DGX 翻译模型。在 `a9b74bd` 上，模拟客户私聊原账号发俄文，飞书测试群收到俄文及中文，群内普通中文回复经翻译后在第二个 Telegram 账号的原私聊中可见。首次飞书群查询失败，现有重试恢复后继续完成验收。

首轮发现桥接未传递 Telegram 发件人显示名。`53e8cb2` 复用渠道姓名解析，将发件人的名和可选姓带入现有飞书渲染。没有姓名时保留“客户”。用原数据库重启后重新执行两端收发，飞书显示测试账号全名，第二个 Telegram 账号再次收到来自原账号的俄文。两轮四次投递各取得平台回执，本机记录见 `artifacts/personal-pilot/mac-live-acceptance.json`。同一提交在 DGX aarch64 / Python 3.12.3 执行 `bash scripts/acceptance.sh`，118 项通过，0 失败、0 错误、0 跳过。源码 SHA256 为 `9c1463d16a927c4082727db855bb96196dadc118777f1841d6b43de1bfe6f41e`，报告在本机 `artifacts/dgx-display-name-20261126/`。

## 新会话接待与飞书群规则

### 基本接待

配置 `mode: personal-managed`，填写已获准的 `telegram.allowed_chat_ids` 与 `feishu.staff_ids`，留空 `feishu.chat_id`。使用单独的 SQLite 文件运行。每个新联系人第一次来信，应用创建对应的飞书私有内部群，加入追踪机器人和指定员工，并给客户自动发送一次固定欢迎语。建群结果未知时保留该状态，等待核对；欢迎语仍按已验证的 Telegram 路由发送。飞书自建应用需要 `im:chat:create` 及上文收发和成员读取权限。

接待收集公司、货物及数量、起点和终点。问询草稿在对应群显示完整对客译文，授权员工复制 `/approve <草稿 ID> <版本>` 逐条批准。审批时核对同群、员工身份、版本及预览送达，出站时再次核对已批准文本。新客户消息使旧的未发送草稿失效，普通人工回复接管会话。`intake.max_replies` 可设 3–5，默认 5；只计有平台发送回执的问询，欢迎语单独计，撤回不会回退轮次。

资料已齐或达到问询上限时，飞书群内广播「AI 接待结束，请转人工」，列出已收资料、客户原话和缺项。带 `@` / `＠` 的消息、群公告、审批指令和群内机器人消息属于飞书侧。自动建群时只广播一次转发规则。

### 客服跟进

`agent.enabled` 默认关闭，修改后重启个人账号服务。基本接待始终开启。开启 Agent 后，客服可在已转人工的同一群发送 `/draft` 生成一条跟进草稿，审批仍使用草稿对应的译文版本。普通员工回复照常翻译并标明是人工客服。配置允许额外内部群机器人时，通过 `feishu.allowed_bot_app_ids` 指定并核对成员名单。

飞书每日文档和会话知识库留在后续范围。[历史会话本体规划](intake-knowledge.md)

先前版本 `425b646` 的 Mac 双向与提及隔离验收见本机 `artifacts/personal-pilot/mac-intake-acceptance.json`，DGX 模拟报告为 `artifacts/dgx-intake-20261126/acceptance.json`。这些记录对应当时的自动接待流程。

## 普通账号找不到连接入口时

已实测 [Telegram Web K](https://web.telegram.org/k/) 可使用已登录的普通账号完成连接。在编辑个人资料中打开 Chat Automation，选择 Only Selected、指定测试联系人、开启 Reply。[客户端源码](https://github.com/morethanwords/tweb/blob/master/src/components/sidebarLeft/tabs/editProfile.tsx#L422-L472)

备选官方 [Telegram Desktop 7.4.6](https://github.com/telegramdesktop/tdesktop/releases/tag/v7.4.6)。[该版本源码](https://github.com/telegramdesktop/tdesktop/blob/v7.4.6/Telegram/SourceFiles/settings/sections/settings_business.cpp#L506-L516)对免费用户开放 Telegram Business 菜单下的 Chatbots，和本机 App Store 原生 Telegram 的菜单实现不同。在两者都不可用时，[TDLib](https://core.telegram.org/tdlib/docs/classtd_1_1td__api_1_1set_business_connected_bot.html)可以通过由本人授权的临时用户会话完成一次账号绑定；该方案尚未在本机测试。

[Vercel Chat SDK 的 Telegram 适配器](https://github.com/vercel/chat/blob/e490e84f01dbf477a5d1a9e7eec16c3fe72a31ac/packages/adapter-telegram/src/index.ts#L1172-L1225)按连接 ID 和聊天 ID 隔离会话并过滤机器人回环。[Chatwoot 开发分支](https://github.com/chatwoot/chatwoot/blob/38258d313d147657be00acb4a5d7f6060422e34d/app/services/telegram/incoming_message_service.rb#L105-L118)持久化连接 ID，[回复时将其放入请求](https://github.com/chatwoot/chatwoot/blob/38258d313d147657be00acb4a5d7f6060422e34d/app/models/channel/telegram.rb#L157-L169)。OmniService 采用同一 Bot API 协议，入站使用本机长轮询。

## DGX Linux 运行

Web K 或桌面客户端只做一次 Telegram 账号授权。DGX 上同一个桥接进程负责 Telegram 长轮询、飞书长连接及双向消息队列；翻译使用 DGX 的 `http://127.0.0.1:11434/v1`。飞书在 Linux 上配置应用身份，按测试群名单预检。`lark-cli` 1.0.99 官方 Linux arm64 二进制已在隔离目录核对 SHA256、版本和消息事件 schema；Linux 应用凭据尚未配置。

DGX 直连 Telegram 超时，飞书 HTTPS 和本机翻译已通。使用 DGX 已配置的美国 SSH 主机，以 `ssh -N -T -o ExitOnForwardFailure=yes -L 127.0.0.1:37892:api.telegram.org:443 <美国主机别名>` 建立转发，再给桥接进程设置 `TELEGRAM_TLS_TUNNEL_PORT=37892`。这是 Telegram 专用转发，程序仍校验 Telegram 的 TLS 证书。若部署改用私网 HTTP CONNECT 代理，可单独配置 `TELEGRAM_HTTPS_PROXY`，两个配置互斥。

已实测从 DGX 经美国主机调用 `getMe` 和 `getWebhookInfo` 成功，且确认 Secretary Mode 开启。测试结束已关闭临时转发。Linux 真实联调仍需为该应用配置飞书凭据并复验双向收发及重启。整个链路只运行一个 Telegram 长轮询实例。

包含美国 SSH 转发支持的提交 `cdb3bfc` 在 DGX 完整模拟验收通过 118 项，源码指纹 `58d0416af863da4c277945077cd2e7ef68148613de75d9b0fc235beb63179a87`。原始报告与日志见本机 `artifacts/dgx-us-egress-20261126/`。
