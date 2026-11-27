# OmniService 团队部署

[观看人工授权演示与章节索引](authorization-walkthrough.md)。

每个实例绑定一个飞书自建应用、一个 Telegram 原账号和一份 SQLite 数据库。可以装在办公室 Linux 或云主机，管理网页使用你自己的域名。互不信任的团队用各自的 Linux 用户或容器隔离。团队代号只用于区分配置目录和服务名。

## 1. 准备 Linux

准备 Python 3.12（含 venv）、systemd 用户会话、已签发 HTTPS 证书的域名。第一次构建固定官方 TDLib 时，Linux 部署机器另需 CMake、C++ 编译器、gperf 及 OpenSSL/zlib 开发库。部署者准备这些系统构建工具即可；操作员不需要单独下载、安装或登录任何 CLI。

在运行服务的普通 Linux 用户下，从仓库执行：

```bash
bash scripts/setup-personal.sh support prepare
```

这个命令一并准备实例、固定校验的飞书 CLI、私有 Python 环境中的飞书 SDK，以及从固定官方源码验证并构建的 Telegram TDLib。安装后验证真实 SDK 入口和 TDLib 的版本、符号；不发起平台授权、不触碰既有运行服务。首次构建可能较久；相同实例后续复用已验证的依赖。默认从 GitHub、PyPI 获取固定内容；可加 `--tdlib-source /path/to/official.tar.gz` 使用相同 SHA256 的官方缓存源码，不接受任意动态库作为托管构建。

安装器输出配置、凭据和数据库的路径。以下示例用团队代号 `support`。同一台主机运行多个实例时，每个管理页使用各自的环回端口和 HTTPS 主机名。反向代理只转发该管理站点至所设置的环回端口，保留浏览器的 `Origin`，并在访问日志中省略 OAuth 回调的查询参数。无需暴露 Python 监听端口。

## 2. 从命令行完成首次接入

安装者先准备实例参数、专用 Telegram Bot Token 和 Telegram 应用的 API ID/hash。使用者按提示在飞书和 Telegram 登录或确认授权，由程序写入配置并验证平台回执。已接入实例的启动和升级沿用保存的配置，无需再次登录 Telegram。

用同一个仓库入口续接；它自动使用该实例的私有组件。Telegram Bot Token 和开发者 [API ID/hash](https://core.telegram.org/api/obtaining_api_id) 分别用于 Bot API 和一次性用户绑定，不随仓库分发。可信部署者可将 API ID/hash 预置为仅本人能读的 JSON（字段 `api_id` 整数、`api_hash` 字符串），或在终端按隐藏输入提示填写。切勿提交到 Git。

```bash
team_config="${XDG_CONFIG_HOME:-$HOME/.config}/omni-service/support"
bash scripts/setup-personal.sh support configure -- \
  --callback-url 'https://support.example.com/admin/auth/callback' \
  --model-url 'http://127.0.0.1:8000/v1' --model 'translation-model' \
  --feishu-app-name 'OmniService 物流客服' \
  --tdlib-app-credentials "$team_config/telegram-application.json" \
  --telegram-owner @example_logistics \
  --chat-scope allowlist --test-chat-id 12345678 --port 8081
```

- **模型**。输入一个 OpenAI 兼容的本机或私网地址和模型名。如网关有 API Key，按隐藏输入提示填写。携带 Key 的非本机地址使用 HTTPS。安装前会用一条合成消息测试中俄翻译。业务来信可能发送到你指定的模型，期限由实例的数据保留安排决定。 [Telegram 业务机器人数据条款](https://telegram.org/tos/bot-developers#5-4-telegram-business)
- **飞书**。新应用由官方 Python SDK 发起创建并预填群消息、附件、建群与成员权限，以及群消息事件和卡片回调。用户扫码或打开官方链接确认后，程序保存 App ID 和 Secret。应用管理员登记相同的 OAuth 回调地址，确认机器人事件及卡片回调走长连接，设置团队可见范围并按组织规则发布和审批。随后用管理员账号完成官方身份授权，程序核对 Open ID 和组织标识。同一实例完成身份验证后，重跑接入命令会沿用已验证的管理员。托管模式自动核对当前应用的客服权限，只将确实缺少的权限放进官方申请链接和二维码，然后暂停接入，等待获授权的人确认；可用 `check-feishu-scopes` 单独复核。用户扫码授予的权限、应用权限、企业发版及事件订阅是不同检查。复用明确指定的应用可加 `--feishu-app cli_...`。 [批量申请和发版审批](https://open.feishu.cn/document/ukTMukTMukTM/uQjN3QjL0YzN04CN2cDN?lang=zh-CN) · [飞书长连接](https://open.feishu.cn/document/server-side-sdk/java-sdk-guide/handle-events)
- **Telegram**。程序用 Bot Token 开启并读回秘书模式，随后显示 Telegram 账号登录二维码。使用新号在「设置 → 设备 → 连接桌面设备」扫码。程序核对预期账号，按参数设置聊天范围和回复权，只向指定机器人发送配对挑战，并验证 Bot API 返回的账号与连接。已有其他机器人时会停止。临时用户会话退出后，后台继续用 Bot Token。平台要求验证码或两步验证时在终端隐藏输入。如果 TDLib 已确认对专用机器人发送成功、仅差 Bot API 回执，同参数重跑可复用已确认的记录。加入发送队列不算成功。明确发送失败不会留待确认记录；结果不明也不会自动重发，核对专用机器人状态后再重跑。旧版未确认的记录不再复用。 [Telegram 聊天自动化](https://core.telegram.org/api/bots/connected-business-bots) · [TDLib 用户侧绑定](https://core.telegram.org/tdlib/docs/classtd_1_1td__api_1_1set_business_connected_bot.html)

`allowlist` 把 Telegram 授权和 OmniService 接收同时限定在给出的测试私聊。实际接管指定类别时用 `--chat-scope connection --telegram-recipients new`，可重复显式选择 `existing`、`contacts`、`non-contacts`，并用 `--telegram-exclude-chat-id ID` 排除特定私聊。获取更多类别前先核实范围。TDLib 登录需要访问 Telegram 用户侧网络，可用 `--tdlib-http-proxy http://127.0.0.1:7890` 指定现有的 HTTP CONNECT 代理。Bot API 的专用 TLS 转发不能代替 TDLib 登录通道。

若需要先完成飞书发布审批，可以单独登记应用。登记只涉及飞书，成功后同一命令可重复运行而不再创建新应用。随后运行上面的 `configure` 续接。

```bash
bash scripts/setup-personal.sh support register-feishu -- --feishu-app-name 'OmniService 物流客服'
```

没有兼容的 TDLib 时，省去上面所有 TDLib 和 Telegram 登录参数，原来的 [客户端授权流程](authorization-walkthrough.md#客户端操作备选) 仍可使用。

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

2026-11-26 在 DGX Linux aarch64 / Python 3.12.3 上对源码 `0910eb6` 执行 `bash scripts/acceptance.sh`，231 项通过，0 失败、0 错误、0 跳过。三公司模拟使用各自的 Telegram 连接和飞书应用，验证同一客户分别收到各自会话中的回复。管理页使用三组模拟配置进行浏览器检查。机器可读结果和日志位于 `artifacts/dgx-companies-20261126/`。
