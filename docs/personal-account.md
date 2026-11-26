# P1 个人账号桥接：开发完成，等待本机账号联调

客户继续添加原有个人账号；员工在飞书对应客户群里正常发言，系统识别客户语言、翻译员工中文并由原账号发送。本阶段已经从契约层接成可运行程序：Telegram 官方 connected-account、飞书官方长连接、本地模型 HTTP、SQLite 持久收发和管理恢复。开发验收使用 Linux、合成客户与模型响应；真实账号和 M6/DGX 的质量验证仍未执行。

交付入口：[PR #4](https://github.com/reknottycat/omni-service/pull/4)，分支 `feature/personal-account-bridge`；后续联调继续用 [Issue #3](https://github.com/reknottycat/omni-service/issues/3) 跟踪。此 PR 基于尚未合并的 PR #2，当前 main 不是完整程序。

## 已实现的运行链路

1. Telegram `getUpdates` 接收原账号连接事件与授权私聊；白名单外消息不写入业务存储、不交给模型。
2. 连接、权限、游标、经过筛选的入站队列和出站队列保存在同一个 SQLite 数据库。事件入队成功后才推进游标。
3. 每位客户有独立飞书服务群。配置的员工自动入群，客户与 AI 消息同步到群。
4. 模型调用在工作线程、数据库写事务之外执行。飞书 SDK 回调只做身份检查和持久入队。
5. 员工发普通中文消息即暂停当前会话 AI，自动翻译后通过 `business_connection_id` 由原账号发出。账号本人在 Telegram 直接回复也会接管，不再次代发。
6. 飞书原生撤回事件可取消未发消息，或用精确发送回执调用 Telegram `deleteBusinessMessages`。
7. 限流按明确等待时间有限重试；超时、回执丢失和不完整回执保持未知，不盲目重发。管理员有核对未知结果、恢复失败建群和重试翻译的入口。

每次外发、重试和模型处理前都重新检查账号、会话、员工与连接状态。恢复进程时会重新核实 Telegram 连接，不能只信任磁盘上的旧权限。确定回执的删除不会被另一条未知发送阻塞。

## 渠道真实接入状态

| 渠道 | 当前代码 | 还需要的外部条件 |
|---|---|---|
| Telegram 原账号 | 官方 connected-account 轮询、代发、撤回、权限及重启恢复已实现 | 你在客户端连接专用 Bot、允许测试联系人，提供本机 Token 配置 |
| 飞书 | 企业自建应用 REST、官方 SDK 长连接、建群、成员、消息和撤回已实现 | 测试组织、自建应用、权限和员工 Open ID |
| WhatsApp 普通个人账号 | P0 Bot/Cloud API 模拟保留；私人号 live 模式明确禁用 | 普通个人账号没有本项目可直接启用的官方路线；Business app 共存与非官方关联设备需要另行选路 |
| MAX 原账号 | P0 Bot 模拟保留；私人号 live 模式明确禁用 | 当前 Open Client API 限制服务端桥接，须平台明确允许并提供适用技术文档 |
| M6 翻译 / DGX AI | 本地 HTTP 客户端、三种翻译协议、知识库及失败转人工已实现 | 实际 URL、协议、模型名，必要时本机 API 密钥及网络可达性 |

Telegram 官方当前允许用户账号连接 Secretary Mode Bot，文档说明无需 Premium；回复权限仍受最近 24 小时有入站消息的私聊限制。飞书长连接不需要公网入口，服务主机仍须主动访问 Telegram 与飞书。

WhatsApp 的官方 Coexistence 是 **WhatsApp Business app 原号码与 Cloud API 共存**，不等于普通 WhatsApp app 直接开放私人消息接口；它还涉及 Embedded Signup、服务商资格及 webhook。当前没有启用非官方设备连接、没有转换你的账号。

MAX 公开 Open Client API 文档要求用户设备直连，限制开发者/第三方后端、代理、网关以及转入机器学习基础设施；完整协议仅对获批开发者开放。这里的阻塞有具体平台依据，不以模拟 Bot 代替原账号接入。

## 本地模型与知识库

`translation` 与 `llm` 可指向不同机器：

- `openai-compatible`：兼容 Chat Completions，base URL 包含 `/v1`。
- `ollama`：使用 `/api/chat`，base URL 为服务根地址。
- `libretranslate`：使用 `/detect` 与 `/translate`，仅用于翻译。

默认仅允许局域网、loopback 和 Tailscale 地址。M6 与服务主机不是同一台时，应填写 M6 的实际局域网/Tailscale 地址，不能填服务主机的 `127.0.0.1`。支持 `structured_output` 的 `json_object`、`json_schema` 或 `prompt` 模式，以适配本地服务的能力；无论模式如何都会严格检查返回值。

可执行知识库为 `fixtures/knowledge/logistics-zh-ru.json`，Markdown 是可读说明。现有七类合成资料涵盖问候、询价资料、单证、运输方式、时效、运单和特殊货物。AI 选择检索出的批准答案，并记录来源 ID 与知识库版本；未知问题、虚构来源或需要人工确认的情况转人工。当前不是自由生成真实运价的模型。

翻译失败不会自动发送原文。数量保持检查采取保守策略，例如数字分组格式发生变化时可能拒绝发送，员工可核对后重试。实际翻译准确性、多语种覆盖、模型延迟须用你的端点验证。

## 开发复验

```bash
bash scripts/acceptance.sh
python scripts/demo_personal.py
python -m pip install -r requirements-live.txt
PYTHONPATH=src python tests/test_feishu_live.py --official-sdk-smoke
```

标准验收和演示均使用 loopback 假平台，无需任何平台凭据。SDK 独立检查使用真实安装的固定版本，替换外部网络连接，验证实际事件模型、protobuf ACK、SQLite 提交与启停。

[开发验收与证据](evidence/personal-acceptance.md)记录最终结果、源码摘要及实际边界；[账号资料与启动步骤](setup.md)是你准备资料时的完整入口。

## 目前的边界

- 本轮未使用真实 Telegram、飞书、WhatsApp、MAX 账号，未连接你的 M6/DGX。
- 二进制媒体只保留元数据和明确的待处理状态；尚未完成图片、语音、文件跨平台上传下载。
- 当前为单进程 SQLite 运行器，有单实例锁和持久队列；多客户测试证明隔离与流程，不代表无限容量或生产并发承诺。
- 翻译/AI 调用有界，但单个工作线程逐条处理；实际吞吐取决于模型速度和平台限流。
- 已在网络途中的消息不能保证在员工接管或撤回时瞬间取消，必须等待平台回执。
- Chatwoot/NextBridge 不作为运行依赖，也没有声称已完成其联调。

## 官方依据

核对日期：2026-11-26。

- [Telegram connected business bots](https://core.telegram.org/api/bots/connected-business-bots)
- [Telegram Bot features](https://core.telegram.org/bots/features#business-bots)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [飞书 Python SDK 事件与长连接](https://open.feishu.cn/document/server-side-sdk/python--sdk/handle-events)
- [飞书官方 Python SDK](https://github.com/larksuite/oapi-sdk-python)
- [飞书应用访问凭证](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/tenant_access_token)
- [WhatsApp Business app Coexistence](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users)
- [MAX Open Client API 条件](https://legal.max.ru/openclient-docs)
- [Ollama Chat](https://docs.ollama.com/api/chat)
- [vLLM 兼容接口](https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html)
- [LibreTranslate API](https://docs.libretranslate.com/api/)
