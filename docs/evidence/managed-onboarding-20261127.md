# OmniService 首次接入修复验证

基线：`43b4471eacc031991032628c454e2b7aa63e8a7e`（`codex/public-release` 的 #7 合并提交）。在 M6 / macOS ARM64 / Python 3.12.16 的隔离目录验证，未接触既有试点身份、数据库或服务。

- 修复前负测：TDLib 仅返回排队，Bot API 无回执。第一次和下一次都超时，共 1 次绑定/发送、1 条持久记录、0 次已确认发送。
- 修复后：`PYTHONPATH=src python3.12 -m unittest discover -s tests -p '*onboarding*.py' -q`，23 项通过。覆盖只接收同一临时消息、会话与 TDLib client 的终态；终态早于 RPC 回应、成功、失败、删除、超时、旧版记录不复用、成功确认后的 Bot API 重试，以及飞书应用权限齐全、权限缺失预填官方二维码、用户 token 权限不能冒充应用权限、依赖校验拒绝。
- 使用官方 [lark-cli v1.0.99 清单](https://github.com/larksuite/cli/releases/expanded_assets/v1.0.99) 对已有 Linux arm64 发布包重新计算摘要，并提取其中唯一目标二进制，长度 45,744,290 B，ELF 文件；未在 Mac 执行这份 Linux 二进制。Linux 系统服务用例在 Mac 按设计跳过。
- 依据 [TDLib 消息成功](https://core.telegram.org/tdlib/docs/classtd_1_1td__api_1_1update_message_send_succeeded.html)、[失败与删除替代](https://core.telegram.org/tdlib/docs/classtd_1_1td__api_1_1update_message_send_failed.html)及同版 JSON 生成器：空对象字段可省略。采用 [飞书 CLI 官方预填申请入口](https://github.com/larksuite/cli/pull/1722) 与应用 scope 读回，不用用户 token 的 scope 判断应用权限。

未使用真实的新账号、飞书组织或管理员权限，未调用配置平台接口；未验证全新发行物自动提供 TDLib、Python SDK、用户授权回跳、管理员发版、事件长连接或真实收发。已通过的旧试点未重跑。
