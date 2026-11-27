---
name: omni-service-onboarding
description: 为 OmniService 的独立团队接入 Telegram 个人账号、专用机器人和飞书自建应用，配置 Linux 服务、处理首次授权或恢复中断的接入，并验收真实双向消息。
---

启动前读[团队部署](../../../docs/personal-deployment.md)和当前检出的 `AGENTS.md`。优先使用仓库的 `scripts/setup-personal.sh` 和 `scripts/install-personal.sh`。真实接入对象是 Telegram 个人账号与飞书团队，其他渠道按各自已验证范围报告。

## 准备实例

- 核对目标源码版本、Linux 主机、团队代号、Telegram 原账号和专用机器人、飞书应用与组织、获准测试账号及接管范围。把用户指定排除的账号从授权范围和验收对象中剔除。优先用一个获准的独立测试账号，按 `allowlist` 在 Telegram 和 OmniService 两侧限制同一组私聊。
- 在目标主机验证 Bot API 的 HTTPS、飞书网络、翻译网关的实际请求和管理页回调路径。Telegram Bot API 与 TDLib 用户登录可能需要不同网络出口，分别验证。使用独立配置、凭据、SQLite、CLI profile、管理端口和回调地址。复用现有实例前先检查实际服务及绑定，避免重复轮询。
- 用 `scripts/setup-personal.sh <team> prepare` 准备依赖与实例。已备齐依赖时可用 `scripts/install-personal.sh <team> --no-start`。凭据使用实例私有文件或隐藏输入。登录链接、二维码、验证码、令牌和用户会话只在当前授权流程中使用，留存证据前剔除这些内容及私人消息、账号和标识。

## 配置与恢复

1. 优先用仓库 CLI 和官方 API。需要先审批时，用 `scripts/setup-personal.sh <team> register-feishu` 登记新应用。按[团队部署](../../../docs/personal-deployment.md#2-从命令行完成首次接入)登记回调、可见范围、消息事件和卡片回调，再按组织规则发布。应用身份预检和管理员授权分别完成。管理员授权后核对该应用的 `open_id` 和 `tenant_key`。只复用同一实例、同一应用由正式流程验证过的管理员检查点。
2. 用 `scripts/setup-personal.sh <team> configure -- ...` 配置模型和授权范围。已有适用的 Telegram 开发者凭据时，可用官方 TDLib 让配置器核对原账号并设置权限。采用单次扫码授权，或原生客户端控件不可用时，走[Telegram Web K 单次扫码流程](../../../docs/telegram-web-k-onboarding.md)，省略 TDLib 和 Telegram 登录参数。由账号持有人扫码并在需要时直接完成两步验证。
3. Web K 从 **Settings → More → Edit Profile → Chat Automation** 进入。先核对原账号、专用机器人和用户批准的范围。指定私聊或全部新私聊都可选 **Only Selected Chats**，按批准范围加入具名测试私聊或 **Included Chats → New Chats**。添加机器人后检查默认开启的权限，逐项关闭本次任务用不到的标已读或删除等权限。保存并重新进入设置回读实际范围。
4. 配置器通过配对挑战和 Bot API `getBusinessConnection` 核对账号、启用状态及回复权。聊天覆盖范围另用 TDLib 或客户端回读。等待平台回执中断后先查已有连接和私有检查点，按原参数恢复读取，同一挑战无需重新发送。确认绑定后退出本次临时客户端会话，随后启动服务。已启用的实例按部署文档升级或迁移。

## 启动与验收

- 用 `onboarding check` 做平台预检，再用安装器启用服务。检查运行版本、systemd 状态、飞书长连接是否就绪和任何实际错误。管理页在本次范围内时，用获准飞书身份走完重定向和管理页回读。
- 让获准的测试账号发送一条独立、易识别的合成询价。在目标飞书实例确认正确的私有服务群、授权成员和译文。按配置批准草稿或从群内发出一条人工回复。在原 Telegram 私聊验证实际收到正确译文，核对平台回执以及批准前后的发送行为。按需求单独验收媒体或重启后续接。
- 用 `bash scripts/acceptance.sh` 在 Linux 复验代码。交付时记录源码版本、环境、命令、测试范围和可查结果。把代码测试、平台预检、真实双向收发和上线状态分别按实际证据列明。用户要求操作录像时只留实际发生的界面步骤，在录屏外完成凭据输入和短期授权码处理。
