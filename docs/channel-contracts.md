# 渠道契约：Telegram、MAX、WhatsApp 与飞书

核实日期：2026-11-26。本文描述本仓库适配器的实际范围。验收采用 Linux 上的模拟 webhook 与本地 HTTP 平台；不要求真实账号、外网回调、真实发送或真实模型。

## 公共接口与归一化

实现：`src/omni_service/channels.py`。共享值对象：`models.py`。

- `parse_events(channel, account_id, payload)` 返回全部 `InboundEvent`，WhatsApp 遍历所有 `entry/changes/messages`。已识别事件结构错误抛 `ChannelError('invalid_payload')`，不透出 `KeyError`。
- 所有来源 ID 存为字符串，时间统一为 Unix 秒。Telegram、WhatsApp 来源为秒，MAX、飞书来源为毫秒。
- 事件类型是 `message/edit/delete`。消息附件种类是 `image/file/audio`，Telegram voice 归一为 audio。未知媒体显式返回 `unsupported_media`，不会作为普通文本自动回复。
- `send_request` / `delete_request` 只构造 `Request`；`path` 是包含 query 的相对路径，平台基址由 `API_BASE_URLS` 给出。此模块无网络调用。
- `parse_response` 验证平台语义成功；发送返回远端消息 ID，删除及加人返回空字符串，建群返回 chat ID。HTTP 200 不等于成功。缺失、格式损坏或无法证实结果的回执返回 `invalid_response`，核心须保留结果未知（unknown），不得据此标记失败或自动重发；只有明确拒绝标志或有效错误代码才算平台拒绝。请求异常与 HTTP 重试由调用者处理。
- `verify_webhook` 使用未改写的原始 body 与大小写不敏感的 HTTP 头验证。验证失败返回 False。
- `CAPABILITIES`（也导出为 `capabilities`）公开发送、媒体、撤回和窗口能力。表中 True 表示适配器实现该操作，仍受平台权限、时限和运行结果约束。

## Telegram

API 基址 `https://api.telegram.org`。文本路径 `/bot<TOKEN>/sendMessage`，字段 `chat_id/text`。媒体分别使用 `sendPhoto`、`sendDocument`、`sendAudio`。Token 属于 URL 路径，不应在日志中暴露。

适配器处理普通 `message`、`edited_message`，忽略 bot echo 和非消息 update。事件去重键为 `account_id + update_id`；来源消息映射另用 `chat_id + message_id`。`from.language_code` 仅是语言提示。

撤回使用 `deleteMessage`，`message_id` 在请求中是整数。普通消息通常要求发送不足 48 小时；机器人可删除自己发送的私聊消息。普通 Bot API 没有通用客户删除事件；本适配器不把 business connection 专用事件能力当普通机器人能力。

Webhook 校验头 `X-Telegram-Bot-Api-Secret-Token`。发送返回必须 `ok=true` 且存在 `result.message_id`；删除还要求 `result=true`。429 响应的 `parameters.retry_after` 转成 `ChannelError('rate_limited', retry_after=...)`。

官方来源：[Bot API](https://core.telegram.org/bots/api)，定位 `Update`、`setWebhook`、`sendMessage`、`sendPhoto`、`sendDocument`、`sendAudio`、`deleteMessage` 和 `ResponseParameters`。

## 俄罗斯 MAX messenger

本项目的 MAX 指 [MAX 官方开发者平台](https://dev.max.ru/docs-api)。核实时官方已要求使用 **`https://platform-api2.max.ru`**。请求头是 **`Authorization: <token>`**，没有 Bearer 前缀。

文本发送 `POST /messages?chat_id=...`，或直接地址 `?user_id=...`，body 为 `text`。成功 ID 在 `message.body.mid`。媒体写在 `attachments`，图片、文件、音频使用目的平台上传返回的 token。

入站 `message_created/message_edited` 的客户身份取 `message.sender.user_id`，不能取 `recipient.user_id`，后者在私聊中可能是机器人。有 `recipient.chat_id` 时保留该会话 ID；没有时以 sender.user_id 作为 direct 地址并设置 `recipient_type=user`。创建事件用消息 ID 去重，编辑事件附时间区分版本。

`message_removed` 为顶层 `message_id/chat_id/user_id/timestamp`，归一为 delete。机器人回显忽略。

撤回用 `DELETE /messages?message_id=...`。必须确认 JSON `success=true`；即使 HTTP 200，`success=false` 仍是失败。私聊只删除机器人自己发送的消息；群聊和频道有管理员及删除权限要求。官方每会话发送、删除分别有限速；模拟结果不代表实际平台吞吐。

Webhook 校验头 `X-Max-Bot-Api-Secret`。生产回调要求 HTTPS 443、有效证书并在 30 秒内返回 200；持续失败有重试与自动退订机制。本地模拟不注册公网 webhook。

官方来源：

- [发送消息](https://dev.max.ru/docs-api/methods/POST/messages)
- [删除消息](https://dev.max.ru/docs-api/methods/DELETE/messages)
- [Update 事件结构](https://dev.max.ru/docs-api/objects/Update/)
- [订阅与 webhook 校验](https://dev.max.ru/docs-api/methods/POST/subscriptions)

## WhatsApp Business Cloud API

发送路径 `/<API_VERSION>/<PHONE_NUMBER_ID>/messages`，请求头 `Authorization: Bearer <token>`。版本默认 `v27.0`，可配置。入站传给解析器的 `account_id` 必须为实际 `metadata.phone_number_id`，避免串企业号码。

文本 body 包含 `messaging_product=whatsapp`、`recipient_type=individual`、`type=text`、`text.body`。手机号使用 `to`；BSUID 使用 **`recipient`**。两者不同时发送。

入站包含所有 `entry[].changes[].value.messages[]`；状态回执 `statuses[]` 不触发 AI。支持无手机号、仅 `from_user_id` 的用户；优先 BSUID，回退 `from`。同一请求中的全部消息都解析，去重依据企业号码加消息 ID。

签名验证为 `X-Hub-Signature-256: sha256=<hex>`；HMAC-SHA256 的 key 是 App Secret，message 是原始请求体字节。JSON 重排或重新编码会改变签名。GET 验证 challenge 由 HTTP 服务层处理。Webhook 可能重送，持久去重由核心负责。

自由文本必须在客户消息打开的 24 小时客服窗口内；窗口外应由核心返回明确的模板需求，不能用 mock 成功掩盖限制。

**本适配器撤回能力为 unsupported。** 核实的官方 Messages API 没有可据此实现已发送消息撤回的接口；删除媒体或模板不能代替删除客户聊天中的消息。调用 `delete_request` 立即返回明确的不支持错误，不生成虚假的 DELETE 请求。

媒体类型 image/document/audio 使用目标平台 media ID；document 可含 filename，image/document 可含 caption。audio 不支持 caption，本实现要求单独发送说明文字，避免静默丢失。

官方来源：

- [Messages API](https://developers.facebook.com/documentation/business-messaging/whatsapp/reference/whatsapp-business-phone-number/message-api)
- [文本发送与 BSUID](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/text-messages)
- [Webhook 消息结构](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages)
- [BSUID 入站字段说明](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview/)
- [Webhook 签名、challenge、批量及重试](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/create-webhook-endpoint/)
- [24 小时客服窗口](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages)

## 飞书员工工作端

入站为 schema 2.0，去重字段 `header.event_id`。`im.message.receive_v1` 的员工 ID 优先 `event.sender.sender_id.open_id`，消息内容由 `message.content` 中 JSON 字符串解析。`sender_type=app/bot` 回显忽略。

`im.message.recalled_v1` 官方结构仅提供 `message_id/chat_id/recall_time/recall_type`，**没有操作员工 ID**。适配器输出 `user_id=''`；核心必须用既有消息映射处理撤回，不得凭空添加操作者，也不得据此撤回任意客户内容。

发送：`POST /open-apis/im/v1/messages?receive_id_type=chat_id`，body 的 `receive_id` 指向服务群，`msg_type` 为 text/image/file/audio；`content` 为对应 JSON 字符串。媒体使用 IM 上传得到的 image_key/file_key。native 媒体无 caption 时必须单独发说明文本。

撤回：`DELETE /open-apis/im/v1/messages/<message_id>`。成功要求 `code=0`。

建群：`POST /open-apis/im/v1/chats?user_id_type=open_id`，`name/user_id_list/chat_mode=group/chat_type=private`。加人：`POST /open-apis/im/v1/chats/<chat_id>/members?member_id_type=open_id`，body `id_list`。返回 `invalid_id_list/not_existed_id_list/pending_approval_id_list` 非空，不能标记全部成员加入成功。

### 飞书 webhook 认证模式

模拟和无 Encrypt Key 的官方明文模式：比对 `header.token`（challenge 为顶层 token）与 Verification Token。

如果出现任何一个签名头，则三个签名头必须齐备，不能退回明文 token：`X-Lark-Request-Timestamp`、`X-Lark-Request-Nonce`、`X-Lark-Signature`。签名是 SHA256(timestamp + nonce + Encrypt Key + raw body)，并非 HMAC。

本模块可以验证 encrypted envelope 的签名，但不实现 AES 解密；`parse_events` 对 `encrypt` 明确返回 unsupported。后续真实加密回调应由官方 SDK 解密再进入解析器。本次 Linux 模拟验收使用明文模式或原文签名样例。事件重放去重仍由核心持久化负责。

官方来源：

- [收发与撤回事件/API 总览](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1)
- [发送消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/create?lang=en-US)
- [回调验证与解密](https://open.feishu.cn/document/event-subscription-guide/callback-subscription/receive-and-handle-callbacks)
- [官方 Python SDK：recall event](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/im/v1/model/p2_im_message_recalled_v1.py)
- [官方 Python SDK：receive event](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/im/v1/model/p2_im_message_receive_v1.py)
- [官方 Python SDK：建群请求](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/im/v1/model/create_chat_request.py)
- [官方 Python SDK：建群 body](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/im/v1/model/create_chat_request_body.py)
- [官方 Python SDK：加人请求](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/im/v1/model/create_chat_members_request.py)
- [官方 Python SDK：加人响应](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/im/v1/model/create_chat_members_response_body.py)

## 媒体、翻译和验收边界

解析器只归一化附件定位信息。跨平台发送前必须下载源媒体并上传到目标平台，再将目标平台 remote_id 交给 `send_request`。不能把 Telegram file_id 直接作为 WhatsApp media ID，也不能把飞书云文档 file_token 当 IM file_key。源 URL 只用于独立传输层；本模块不主动下载任何 URL。

语言提示不等于客户本次消息语言。自动识别、会话语言记忆、员工译文、AI 接管由核心及翻译接口验证；fixture 中的文字不构成真实模型质量证据。

渠道单测命令：

```bash
PYTHONPATH=src python -m unittest discover -s tests -p test_channels.py -v
```

`fixtures/channels/` 全部是自行构造的模拟数据，未使用真实客户。单测覆盖输入结构、批量、身份、秒/毫秒、回显、媒体归一化、请求形状、签名、语义失败、BSUID、撤回能力差异和建群/加人响应。完整 HTTP 闭环、语言路由、媒体迁移、并发、重启与跟踪状态以主验收报告为准。
