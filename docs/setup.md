# 账号资料、启动和联调清单

适用分支：`feature/personal-account-bridge`，交付 [PR #4](https://github.com/reknottycat/omni-service/pull/4)。开发验收已经可以在 Linux 用合成数据完成；以下是你准备真实测试账号时的操作入口。没有启动任何真实账号连接。

## 你需要准备什么

| 项目 | 可以交给开发人员的非密钥信息 | 只保存在部署主机的秘密 | 你需要在自己客户端完成的操作 |
|---|---|---|---|
| Telegram | 原账号数字用户 ID、测试联系人 chat ID、专用 Bot 用户名 | BotFather 给出的 `TELEGRAM_BOT_TOKEN` | 原账号保持登录；创建专用 Bot、开启 Secretary Mode；原账号连接它，只允许选定测试联系人，并授权回复和删除 Bot 自己发出的消息 |
| 飞书 | 自建应用 `App ID`、员工 `Open ID` 和显示姓名 | `FEISHU_APP_SECRET` | 测试组织管理员创建应用、启用机器人和长连接，开权限并发布；员工加入可用范围 |
| M6 翻译 | 服务的局域网/Tailscale URL、API 类型、模型 ID、是否要求鉴权 | 如需要：`TRANSLATION_API_KEY` | 保持模型服务运行，确保主服务机器能访问 |
| DGX Spark AI | 同上；暂时没有也可以先关闭 AI、验证人工翻译链路 | 如需要：`LLM_API_KEY` | 准备服务后开启 `llm.enabled` |
| 主服务 | Linux/macOS 机器、Python 3.12+、到 Telegram/飞书及模型的网络 | 随机生成的 `OMNI_ADMIN_TOKEN` | 先只用测试联系人运行 |

无需提供个人密码、短信验证码、Telegram session 文件或 Facebook 登录密码。员工无需分别登录客户的 Telegram。官网仍然可以只留下你的原账号。

WhatsApp 和 MAX 目前不需要交登录资料：普通 WhatsApp 原号路线尚待选择，MAX 原号方案需要平台允许对应场景。具体限制见[个人账号说明](personal-account.md#渠道真实接入状态)。

## 1. 安装与无账号验收

```bash
git clone https://github.com/reknottycat/omni-service.git
cd omni-service
git checkout feature/personal-account-bridge
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[live]'
bash scripts/acceptance.sh
python scripts/demo_personal.py
PYTHONPATH=src python tests/test_feishu_live.py --official-sdk-smoke
```

标准验收与演示不要求安装 live 依赖；实际飞书长连接和 SDK 独立检查需要固定版本 `lark-oapi==1.9.0`。正式安装建议在新的虚拟环境进行。无公网监听要求；机器需要主动访问平台和本地模型。

## 2. 准备本机配置

```bash
cp config/personal.example.json config/personal.local.json
cp .env.example .env.local
chmod 600 .env.local
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

将最后一条命令在你自己终端生成的值写入 `.env.local` 的 `OMNI_ADMIN_TOKEN`。其他密钥也只填到该本机文件。程序按字面值读取 `KEY=VALUE`，不执行 shell 替换；无需 `source` 密钥文件。

编辑 `config/personal.local.json`：

- `mode` 初始为 `disabled`，配置和授权完成后才改 `live`。
- `telegram.expected_owner_id` 是原个人账号的数字 ID，**不是 Bot ID，也不是手机号码**。
- `telegram.allowed_chat_ids` 只填写批准接入的测试/客户私聊 ID；空列表默认拒绝。
- `feishu.app_id` 是创建机器人的应用 ID。
- `feishu.members` 每个员工填写该应用下的 `open_id` 与姓名；可先 1 人，随后补齐 6–7 人。
- `translation` 填 M6 模型的真实地址、协议和模型名。
- `llm.enabled=false` 时先走人工；准备好 DGX 服务后填相应配置再开启。
- `knowledge_path` 指向 JSON 知识库或其同名 Markdown；当前默认是合成测试资料。
- `data_dir` 保存运行数据库，相对路径以配置文件所在目录为准。

`config/*.local.json`、`.env.local`、`var/` 和数据库均在 `.gitignore` 内。配置 JSON 不接受内联密钥字段。

## 3. 获取 Telegram 测试 ID

先在 Telegram 手机/桌面客户端登录原账号，创建专用 Bot 并开启 Secretary Mode，再在原账号设置中连接该 Bot。只开放测试联系人，不要把所有私人聊天整体授权进去。

在主服务尚未启动时运行：

```bash
python -m omni_service.onboarding --env-file .env.local telegram
```

输出原账号的 `owner_user_id`、连接状态和权限元数据。如果没有连接事件，可在客户端断开后重新连接这个**专用测试 Bot**再检查。

确认 owner ID 后，请已授权的测试联系人发一条测试消息，再运行：

```bash
python -m omni_service.onboarding --env-file .env.local telegram --owner-id 你的数字用户ID
```

此时输出该 owner 连接下的私聊 ID，选择测试 chat ID 填入配置。工具不显示消息正文、不代发消息、不主动删除已有 webhook，也不把更新游标推进到读到的消息之后。不要与正式轮询进程同时运行。

如果出现 `webhook_conflict`，说明这个 Bot 已被其他程序使用；应换专用 Bot，或由你检查原有程序后自行调整。工具不会自动破坏已有连接。

Telegram 官方依据：[connected business bots](https://core.telegram.org/api/bots/connected-business-bots)、[Secretary Mode](https://core.telegram.org/bots/features#business-bots)、[Bot API](https://core.telegram.org/bots/api)。当前文档说明连接此类 Bot 无需 Premium；能否回复和删除仍由授权及平台窗口决定。

## 4. 配置飞书和员工

使用**企业自建应用机器人**。群里的自定义 webhook 通知机器人不能替代应用机器人的接收能力。

在飞书开放平台配置：

1. 启用机器人能力，把测试员工加入应用可用范围。
2. 事件订阅选择长连接。
3. 订阅 `im.message.receive_v1` 与 `im.message.recalled_v1`。
4. 根据控制台当前权限说明开通下列能力，并发布应用版本。

| 权限/能力 | 用途 |
|---|---|
| `im:message.group_msg` | 接收服务群普通员工消息，不要求每句 @机器人 |
| `im:message:send_as_bot` | 将客户消息、AI 回复和结果通知送到群 |
| `im:message:readonly` 或事件要求的等效权限 | 接收消息及撤回事件 |
| `im:chat:create` | 为客户创建服务群 |
| `im:chat.members:write_only` | 添加配置员工 |
| `im:message:recall` 或接口允许的等效权限 | 删除由应用发送的镜像消息 |
| `im:message.p2p_msg:readonly`（仅准备员工 ID 时） | 接收员工主动发给应用的设置消息 |

权限需由组织管理员批准，实际以控制台当前可选项及应用发布状态为准。仅开 @消息权限无法满足“员工普通发言直接转发”的需求。

为了少手工查 ID，程序提供员工准备工具。主服务未运行时执行：

```bash
python -m omni_service.onboarding --env-file .env.local feishu --app-id cli_你的应用ID --count 7
```

让每个员工在与该应用的**单聊**中主动发送 `/omni setup`。终端仅输出该员工的 `open_id`；填入配置及姓名。其他单聊、群聊及普通正文不会显示或保存。收齐人数后工具退出；也可 Ctrl+C。不要同时运行设置工具与正式长连接服务，因为飞书会在多个连接间分配事件。

员工以后只需打开系统生成的对应客户群，正常发中文，原生撤回自己的消息即可。员工首次发言会暂停该客户 AI；恢复 AI 由管理命令显式执行。

官方依据：[长连接与事件](https://open.feishu.cn/document/server-side-sdk/python--sdk/handle-events)、[消息 FAQ](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/guide/faq)、[应用机器人与自定义机器人的差别](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)、[官方权限名称表](https://open.feishu.cn/document/develop-robots/add-bot-to-external-group)。这里客户留在 Telegram，飞书群成员是你的员工。

## 5. 接入 M6 与 DGX 模型

配置示例中的地址只作形状说明：

```json
{
  "api_kind": "openai-compatible",
  "base_url": "http://192.168.1.20:8001/v1",
  "model": "实际模型ID",
  "api_key_env": "TRANSLATION_API_KEY",
  "timeout_seconds": 15,
  "structured_output": "json_object",
  "allow_remote": false
}
```

M6 的地址应从运行主服务的机器实际可访问。Ollama 用 `api_kind=ollama` 且 URL 不包含 `/v1`；LibreTranslate 用 `api_kind=libretranslate`，不需要模型名。未知 API 不会被当作兼容接口悄悄使用；提供协议说明或一份去掉密钥的调用样例后再适配。

翻译模型和 AI 模型可以不同、分别运行。没有配置 AI 仍可通过员工人工回复验证翻译链路。当前知识库是测试问答；正式报价、时效、货物限制要换成你审核过的批准答案。

## 6. 检查与启动

```bash
python -m omni_service.runtime --config config/personal.local.json --env-file .env.local check
```

该命令只检查本机配置、密钥是否存在、知识库和 SDK，不发网络请求。`check` 成功不等于账号已接通。

授权与配置完成，把 JSON 中 `mode` 改成 `live`，再启动：

```bash
python -m omni_service.runtime --config config/personal.local.json --env-file .env.local run
```

启动时读取 Telegram Bot 身份、检查 webhook 冲突、核实历史连接，并校验飞书应用访问。默认管理接口在 `http://127.0.0.1:8090`，不接收公网请求。首次授权后，由白名单中的测试联系人发送消息，即可创建飞书客户服务群。

Linux 后台运行可参考 `deploy/omni-service.service`。这是安装模板，尚未在你的机器部署。按实际目录修改，创建专用 `omni` 用户并赋予程序目录读取、`var/` 写入和 `.env.local` 读取权限后再安装。服务默认停止期限 75 秒，运行器响应 SIGTERM，并让当前有界请求结束后保留未处理队列。

## 7. 日常查看和恢复

```bash
python -m omni_service.control --env-file .env.local status
python -m omni_service.control --env-file .env.local list
python -m omni_service.control --env-file .env.local show conv_会话ID
python -m omni_service.control --env-file .env.local handoff conv_会话ID
python -m omni_service.control --env-file .env.local resume conv_会话ID
python -m omni_service.control --env-file .env.local retry-room conv_会话ID
```

`retry-room` 只适用于明确失败的建群，例如修正漏配权限后重新建立；结果未知的建群必须先核对，不能直接重试。

翻译服务恢复后，重试先前未发送的消息：

```bash
python -m omni_service.control --env-file .env.local retry-translation conv_会话ID msg_消息ID --actor-id ou_员工OpenID
```

对于 `unknown`，先在实际平台确认是否产生消息。确认已发送时提供该消息 ID 与核对记录；确认未发送则用 `--delivered no`，该任务会取消，随后员工可重新发送。禁止把猜测当回执。

```bash
python -m omni_service.control --env-file .env.local resolve 任务数字ID --delivered yes --external-id 平台消息ID --evidence '人工核对记录'
```

一致性备份：

```bash
python -m omni_service.runtime --config config/personal.local.json backup --output /你的备份目录/omni-日期.sqlite3
```

备份包含已授权会话内容，应留在你控制的存储。恢复时停止服务，以备份替换对应 `state.sqlite3`，清理同数据库旧 WAL/SHM 前先确认服务确已停止，并保留原库；使用同一原账号和飞书应用配置启动。不要把备份文件放进 Git。

## 真实联调时逐项确认

开发测试已经覆盖逻辑。获得账号后只需执行下列真实场景，不必从头开发：

- 白名单测试客户发俄语，飞书出现正确客户群及中文译文。
- 员工 1 和员工 2 普通回复，原 Telegram 账号显示正确俄文与署名；随后补测其余员工。
- 员工撤回未发/已发消息，检查客户侧实际结果及后台状态。
- 在原账号直接回复，AI 暂停且不回环。
- 翻译服务暂不可用时消息不误发原文，恢复后显式重试成功。
- 重启主程序后会话和队列恢复；重复事件不重复发。
- 撤销连接或移除测试 chat ID 后停止处理该会话。

真实结果归档到 Issue #3；测试账号、实际模型质量、生产容量和平台批准状态分别记录。
