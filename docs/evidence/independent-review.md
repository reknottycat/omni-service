# 独立复核记录

目标：按用户确定的 Linux 模拟边界验证三渠道客服行为，不增加真实账号或生产上线门禁。

复核者：独立子代理，OpenAI GPT-6 Astra Pro，**同厂商**。未宣称 pstack 默认的跨厂商审查 panel 已执行。审查者只读源码和契约，并用本地 HTTP 假平台复现用户可见行为；实现负责人和协调负责人修复后运行冻结源码上的统一验收。

| 发现 | 复现与影响 | 修复 | 最终证据 |
|---|---|---|---|
| 撤回先到，原消息后到 | MAX/飞书未知删除未保存；迟到消息会触发 AI 或把员工已撤回正文发给客户 | SQLite tombstone 按渠道、账号、会话、源消息 ID 持久保存；后到消息/编辑返回 `suppressed_deleted` | `test_customer_delete_before_message_survives_restart_and_is_message_scoped`、`test_staff_recall_before_receive_survives_restart_without_forwarding`；审查者独立复跑确认修复 |
| 残缺回执误作明确失败 | 假平台已发送 Telegram 消息或已删除 MAX 消息，响应改为 `{}` 后服务原来显示 failed | 只有明确拒绝才失败；缺失/异常成功字段为 `invalid_response`，核心保留未知状态 | `test_receipts.ReceiptAcceptance` 3 项 + 渠道坏回执测试，全通过 |
| 429 异常字段使工作线程抛异常 | `parameters:null` 或 `parameters:[]` 导致 `.get` 异常 | 类型检查后读取，回退到 Retry-After 或默认等待 | `test_null_or_array_retry_parameters_use_header_without_crashing`，通过 |

审查者同时指出两个应明确的边界：已经进入网络发送的 AI 请求不能保证取消；测试期间修改文件会破坏证据对应。前者写入合同，后者由源码冻结以及验收脚本前后指纹比较解决。

最终统一运行结果为 87 项通过，0 失败、0 错误、0 跳过；见 [原始报告](local-run.json) 和 [验收记录](linux-mock-acceptance.md)。三项修复均在同一成功运行中获得证据。没有为模拟验收增加媒体二进制、真实模型或生产账号门禁。

需要关注：远端 GitHub Actions 尚未通过；完整媒体和真实模型/平台接入尚属后续阶段；在途 AI 请求按实际回执处理。该结论不把这些边界冒充已验证的生产能力。
