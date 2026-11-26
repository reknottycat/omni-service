# OmniService 团队部署

[观看人工授权演示与章节索引](authorization-walkthrough.md)。

每个实例绑定一个飞书自建应用、一个 Telegram 原账号和一份 SQLite 数据库。可以装在办公室 Linux 或云主机，管理网页使用你自己的域名。互不信任的团队用各自的 Linux 用户或容器隔离。团队代号只用于区分配置目录和服务名。

## 1. 准备 Linux

准备 Python 3.12、systemd 用户会话、官方 `lark-cli` 和已签发 HTTPS 证书的域名。初次扫码创建飞书应用时，安装可选的官方 Python SDK。复用现有飞书应用可以跳过 SDK。 [飞书 CLI](https://github.com/larksuite/cli#quick-start) · [飞书扫码注册](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/README.md#one-click-app-registration)

在运行服务的普通 Linux 用户下，从仓库执行以下命令。

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-onboarding.txt
bash scripts/install-personal.sh support --no-start
```

安装器输出配置、凭据和数据库的路径。以下示例用团队代号 `support`。同一台主机运行多个实例时，每个管理页使用各自的环回端口和 HTTPS 主机名。反向代理只转发该管理站点至所设置的环回端口，保留浏览器的 `Origin`，并在访问日志中省略 OAuth 回调的查询参数。无需暴露 Python 监听端口。

## 2. 运行首次接入

```bash
team_config="${XDG_CONFIG_HOME:-$HOME/.config}/omni-service/support"
PYTHONPATH=src python -m omni_service.onboarding configure \
  --config "$team_config/personal.json" \
  --secrets-file "$team_config/credentials.env" \
  --callback-url 'https://support.example.com/admin/auth/callback' \
  --model-url 'http://127.0.0.1:8000/v1' --model 'translation-model' \
  --chat-scope connection --port 8081
```

- **模型**。输入一个 OpenAI 兼容的本机或私网地址和模型名。如网关有 API Key，按隐藏输入提示填写。携带 Key 的非本机地址使用 HTTPS。安装前会用一条合成消息测试中俄翻译。业务来信可能发送到你指定的模型，期限由实例的数据保留安排决定。 [Telegram 业务机器人数据条款](https://telegram.org/tos/bot-developers#5-4-telegram-business)
- **飞书**。新应用用飞书扫一扫创建，注册流程预填机器人所用的群消息、附件、建群与成员权限，以及群消息事件和卡片回调。程序保存 App ID 和 Secret。应用管理员在飞书设置同一个 OAuth 回调地址、机器人长连接和团队可见范围，发布版本，处理组织要求的审批。创建应用的扫码与管理者确认身份是两次独立授权。程序回读登录账号的 Open ID 和组织标识。复用应用时加 `--feishu-app cli_...` 并按提示输入 App Secret。 [批量申请和发版审批](https://open.feishu.cn/document/ukTMukTMukTM/uQjN3QjL0YzN04CN2cDN?lang=zh-CN) · [飞书长连接](https://open.feishu.cn/document/server-side-sdk/java-sdk-guide/handle-events)
- **Telegram**。首次提供一枚专用机器人的 Bot Token。代码发现机器人、开启并回读秘书模式。原账号扫码打开机器人并点「开始」，随后在 Telegram「设置 → 聊天自动化」选择该机器人、需要接管的聊天并授权回复。程序核对配对发件人与官方连接同属一个账号。选择 `connection` 代表授权范围以内的私聊及文件会进入该飞书实例。仅测指定用户时改用 `--chat-scope allowlist --test-chat-id ID`。现有连接回执已过期时，须在 Telegram 中重新保存该机器人的授权，使平台发出新回执。首装使用专用机器人，不清除其他程序的 Telegram 队列。 [Telegram 聊天自动化](https://core.telegram.org/api/bots/connected-business-bots)

中断飞书授权后重跑同一条命令，可以复用已写入的应用。凭据存于当前 Linux 用户独有的 `credentials.env`。每行使用作为纯文本读取的 `KEY=value`，引号、空白、注释尾缀及 shell 表达式都不适用。通过美国出口走现有 SSH 转发时，可在同一个私有文件填写 `TELEGRAM_TLS_TUNNEL_PORT`，也可单独选择私网 HTTP CONNECT 代理 `TELEGRAM_HTTPS_PROXY`。

## 3. 启动并邀请客服

完成平台授权后运行 `bash scripts/install-personal.sh support`。安装器预检 Telegram 当前回复权和飞书应用身份，然后启动 `omni-service-support.service`。长期无人登录的服务器可由服务器管理员为该运行用户配置 systemd linger。

从 `https://support.example.com/admin/` 使用飞书登录。在「接入与团队」生成一次性链接，发给选中的员工。员工用目标飞书组织的身份打开，点击接受，管理员按回传身份核对并批准。已知道精确 Open ID 时，可指定该账号并通过飞书机器人发送给他本人，这类邀请由匹配身份接受后加入。邀请默认有效 24 小时，待确认期间无客服权限。员工在应用可见范围内才能收到机器人私聊。 [飞书发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)

获准员工加入新会话默认名单。「系统默认」可以设一个固定负责人，也可以让名单内客服按顺序轮流负责新会话。会话负责人会在该会话交接时收到通知。已有客户群及负责人维持原绑定和分配。启动确认后，用一位授权范围内的测试客户发一条消息，核对飞书建群、译文、双向回复及媒体回执，再将范围开放到计划接管的客户。

跨组织协作使用同一个邀请入口和逐人的平台身份判定。正式启用需验证外部共享机器人返回的 Open ID、飞书外部群的实际属性及成员回执、管理员授予该会话的客服权限。只在某个会话实际分配了获准的外部同事时使用外部群。已有内部群增加外部同事时为这一会话单独新建或绑定外部群，历史消息和附件继续留在原群。当前可运行路径覆盖本组织邀请，外部账号的混合名单、群路由、回复、附件和审批回执待实测。 [飞书外部共享机器人](https://open.feishu.cn/document/develop-robots/add-bot-to-external-group?lang=zh-CN)

## 更新、迁移与检查

```bash
bash scripts/install-personal.sh support
systemctl --user status omni-service-support.service
```

升级保留账号配置、凭据和 SQLite 数据，使用新版本之前先检查平台。迁移时先停旧轮询实例并备份 SQLite 和对应配置。现有配置的凭据变量使用 `TELEGRAM_BOT_TOKEN`、`OMNI_FEISHU_APP_SECRET`、`OMNI_ADMIN_SIGNING_KEY` 和可选 `TRANSLATION_API_KEY`，迁移自定义名称时同步改名。将已配置的文件用安装器 `--config`、`--secrets-file` 和 `--no-start` 导入后，用同一配置路径运行 `onboarding prepare-feishu` 重建独立 CLI 应用配置，然后按原路径放回备份并启动。安装器不会替换不同内容的已有配置或凭据。不要用初次接入命令覆盖已启用的账号绑定。

## 2026-11-26 验收记录

| 任务 / 状态 / 负责人 | 交付及范围 | 复验 |
|---|---|---|
| P1-I / done / 协调负责人 | `scripts/install-personal.sh`、`deploy/`、`onboarding.py` 完成单团队部署、凭据隔离、预检和升级激活回滚 | DGX 安装器测试检查 systemd unit、原数据、预检失败以及模拟重启失败回滚。 |
| P1-J / done / 协调负责人 | Telegram 连接身份证明、飞书官方扫码注册及管理员身份引导、模型预检、蓝白管理页、组织内客服邀请和轮流交接 | `test_onboarding.py`、`test_feishu_provision.py`、`test_staff_invites.py`。Mac 浏览器用合成身份验证生成一次性邀请，并检查窄屏和 1210 px 的布局。 |
| P1-K / verify / 协调负责人 | 真实新组织扫码、回调登记、应用发版审批、员工私聊邀请和混合名单外部群 | 用获准外部测试账号核对飞书返回的身份、入群和授权三项回执。现有证据是平台模拟、历史本组织试点与官方协议核对，真实验收待做。 |

DGX Linux aarch64 / Python 3.12.3 对 `8d596ab` 执行 `bash scripts/acceptance.sh`，227 项通过，0 失败、0 错误、0 跳过。报告及首次接口兼容失败记录在 `artifacts/dgx-onboarding-20261126/`。 [Linux 执行记录](evidence/linux-mock-acceptance.md#2026-11-26-团队部署与接入复验)

## 多公司和多个 Telegram 原账号

每家公司使用独立的 Telegram 原账号、专用机器人、飞书自建应用和数据库。不同公司可以选择不同的模型和客服名单。分别执行安装与首次配置，给每家公司分配不同的公司代号、管理端口和 HTTPS 地址。配置时可增加 `--company-name '公司名称'`，新建飞书服务群会带上该名称。此前已建群保留原名。

```bash
bash scripts/install-personal.sh company-a --no-start
bash scripts/install-personal.sh company-b --no-start
bash scripts/install-personal.sh company-c --no-start
PYTHONPATH=src python3.12 -m omni_service.company_accounts list
PYTHONPATH=src python3.12 -m omni_service.company_accounts serve
```

最后一条命令会输出仅当前机器可访问的公司管理页地址。打开页面可查看当前 Linux 用户下各公司的服务状态，并进入各自的管理台。管理台继续使用对应公司飞书组织登录，客服权限、欢迎语、消息、附件和会话分别保存在各公司实例中。公司管理页显示本机 systemd 状态，平台授权状态以各公司的管理台和预检为准。

每个实例启动前检查同一 Linux 用户下启用的公司配置。Telegram 机器人、原账号、业务连接、飞书应用、管理端口及管理回调地址发生重复时，相关实例会报告冲突，避免共用轮询和事件接收。各公司数据目录保持隔离。互不信任的公司应使用不同 Linux 用户或容器，此时公司列表仅显示当前 Linux 用户的实例，跨用户的重复配置不在自动检查范围内。

复验 `PYTHONPATH=src python3.12 -m unittest discover -s tests -p test_company_accounts.py -v`。三公司模拟使用同一个客户 ID 分别建立独立会话，覆盖公司间群路由、配置冲突、权限文件和管理页本机访问检查。真实多公司平台授权和同时在线尚待对应公司账号的验收。
