# P0 Linux 模拟验收记录

## 结论

**按用户确定的 Linux 模拟验收标准，P0 已完成。** 2026-11-26 10:55（北京时间）的统一运行通过 **87 项测试，0 失败、0 错误、0 跳过**。没有使用真实平台账号、真实客户数据或真实模型。

GitHub 远端 CI 单独记录为**未通过**：任务在执行步骤前结束，未获得 runner，日志不存在。当前证据不能确定根因，不能据此猜测余额、配额或代码错误。本轮不将此状态改写成通过，也不把真实账号或远端 CI 追加为用户未要求的模拟验收门禁。

## 版本与证据完整性

- 已验证源码提交：[75a2cd32064c58eebd76fe7e973b749ade5a62a4](https://github.com/reknottycat/omni-service/commit/75a2cd32064c58eebd76fe7e973b749ade5a62a4)。
- Git tree：`79106f3731b7176938963c5b7aeff0bd269d64b1`。
- 验收源码指纹：`1449bf83349219174f7c6e7c2bea05dfbeaed339d27c4601651986e7f012daa8`。
- 测试开始与结束的源码指纹一致，`source_consistent=true`。
- 测试在提交前执行，因此原始 JSON 中 `source_commit` 保留为 `uncommitted`，没有事后篡改成已测试远端提交。随后逐一比较本地文件的 Git blob SHA 和远端 tree：**42 个文件全部一致**。归档提交仅补充文档和证据，不修改已测源码、测试或工作流。
- 逐文件核对：[source-tree-verification.json](source-tree-verification.json)。
- 原始机器报告：[local-run.json](local-run.json)。
- 完整测试输出：[local-run.log](local-run.log)。
- 三渠道 HTTP 演示记录：[demo.json](demo.json)。

## 实际运行环境与命令

| 项目 | 记录 |
|---|---|
| 系统 | Linux 6.18.57，x86_64 |
| Python | 3.12.14 |
| SQLite | 3.53.1 |
| 外部依赖 | 无；运行及测试使用标准库 |
| 平台替身 | 本机 loopback HTTP 服务，记录实际收到的请求与消息 |
| AI / 翻译 | `deterministic-fixtures`，中/俄/英/西固定模拟语料 |
| 统一测试用时 | unittest 输出 23.450 秒；报告含编排 23.521 秒。该数字不是容量或延迟基准 |

```bash
bash scripts/acceptance.sh
python3 scripts/demo.py
```

统一脚本本身也用子进程执行演示命令，验证三个渠道完整对话的返回值和记录文件。进程恢复测试实际终止并重启 Python 服务，读回同一 SQLite 文件后继续处理队列，随后重放 webhook 验证没有重复外发。

## 测试分布

| 模块 | 项数 | 主要观察结果 |
|---|---:|---|
| `test_channels` | 31 | 官方格式解析、请求方法/地址/认证、媒体元数据、坏回执、撤回能力 |
| `test_service` | 27 | 持久会话、成员/语言/AI状态、去重、错误/重启/撤回竞态 |
| `test_http` | 11 | 公开 HTTP 完整链路、1,000 客户、7 员工、实际进程重启 |
| `test_language` | 5 | 消息语言优先、短句记忆、模拟译文和未知短语拒绝 |
| `test_transport` | 9 | 真实 loopback 请求、禁止外网/重定向、429/异常/未知结果 |
| `test_receipts` | 3 | 假平台已经执行操作但回执丢失时，不伪报失败或成功、不重复发送 |
| `test_demo` | 1 | 文档中的演示命令能直接运行并生成三渠道记录 |
| **合计** | **87** | **全部通过** |

## 逐项验收回链

测试名称见原始 JSON 的 `cases[].test`。下表中的类/方法均存在于已核对的源码提交。

| AC | 结果 | 对应行为证据 |
|---|---|---|
| AC-01 | PASS | `test_demo.DemoAcceptance`；`test_http.ProcessRestartAcceptance`；环境与源码清单 |
| AC-02 | PASS | `test_http.HTTPAcceptance.test_three_channels_auto_reply_then_seven_staff_translate_without_send_modes`；`test_channels` 请求形状 |
| AC-03 | PASS | `test_1000_simulated_customers_are_isolated_under_one_account_per_channel`；`test_account_scope_and_unmapped_or_unapproved_staff_have_no_side_effect` |
| AC-04 | PASS | 三渠道 7 员工普通消息自动转发；非成员被拒；`test_service.test_nonmember_cannot_reply_or_recall` |
| AC-05 | PASS | `test_language_is_from_customer_text_and_short_numbers_do_not_change_it`；`test_language.LanguageTests` |
| AC-06 | PASS | `test_automatic_handoff_creates_one_room_and_manual_mode_suppresses_ai`；未知知识请求转人工 |
| AC-07 | PASS | `test_manual_takeover_cancels_ai_and_resume_reenables_it`；恢复后的 AI 答复同步出现在飞书 |
| AC-08 | PASS | `test_replay_batch_status_only_and_bsuid`；`test_duplicate_and_conflicting_event_have_observable_distinct_results` |
| AC-09 | PASS | `test_queued_message_survives_actual_linux_process_restart_and_replay`；错序语言及中断 unknown；撤回先到的持久记录 |
| AC-10 | PASS | `test_queued_message_can_be_cancelled_on_all_channels`；HTTP 原生飞书撤回 |
| AC-11 | PASS | `test_sent_recall_supported_and_whatsapp_unsupported`；`test_max_semantic_delete_failure_and_whatsapp_unsupported_are_not_success`；坏删除回执 |
| AC-12 | PASS | `test_rate_limit_respects_retry_after_and_stops_after_three_attempts`；`test_transport`；`test_receipts` |
| AC-13 | PASS | WhatsApp BSUID 真实出站只使用 `recipient`；完整批次/status-only 处理 |
| AC-14 | PASS | `test_whatsapp_window_is_checked_again_at_dispatch`；`test_whatsapp_window_checked_both_when_queued_and_when_sent` |
| AC-15 | PASS | HTTP 无效认证、账号不匹配、非成员无外发；三渠道演示；源码与证据一致 |

## 规模观察与演示结果

HTTP 测试创建 **1,000 个不同客户会话**，分布在 Telegram / MAX / WhatsApp 三个平台，每个平台使用一个模拟官方账号，以 12 个请求工作线程送入。假平台实际收到恰好 1,000 条回复，客户目标集合与输入一一对应，没有串会话或重复外发。这是当前样本规模的正确性检查，**不代表 1,000 并发在线用户的生产容量承诺**。

每个渠道还单独验证了 7 位员工在同一飞书会话中回复。中文“您好，我来帮您处理。”自动成为俄语正文“Здравствуйте! Я помогу вам.”，带员工署名发送给正确客户。Telegram/MAX 的对应消息被假平台确认删除；WhatsApp 返回 `unsupported` 且未伪造删除请求。

## 独立复核及修复

采用同厂商独立子代理复核，没有宣称执行跨厂商 panel。完整发现和修复证据见 [independent-review.md](independent-review.md)。三项具体发现均已进入本次成功测试集：

1. 撤回事件先到、原消息晚到：持久化精确到消息的撤回记录，晚到消息/编辑不会重新外发；重启后仍生效。
2. 平台已经执行操作、回执为空或损坏：保持 `delivery_unknown` / 队列 `unknown`，不当作明确失败或成功，不盲重发。
3. 429 的 `parameters` 为 `null` 或数组：使用有效响应头或默认等待，返回可处理的限流错误，避免未处理异常。

## 远端 CI 与剩余边界

首次源码提交触发的 [GitHub Actions 运行](https://github.com/reknottycat/omni-service/actions/runs/45745662203) 返回 `failure`。Job `139660956297` 的 `steps=[]`、`runner_name=''`，日志读取返回 404 `BlobNotFound`。连接器不能读取该 check 的 annotations；浏览器未登录私有仓库，未获得进一步原因。已在进度中登记为独立 CI 阻塞，不修改其状态、不擅自支付或变更账户权限。

本轮没有证明真实账号接通、翻译质量、真实 LLM/RAG、生产容量或完整媒体转存。人工接管取消未发送 AI 任务，并停止生成新自动回复；已经进入网络发送的 AI 请求只能按实际回执处理。媒体保持 `media_pending`；未知投递需核对，当前没有生产级自动对账恢复界面。后续完整范围保留在验收合同 B-01 至 B-10。

## 2026-11-26 Telegram–飞书附件与客户指令防护复验

DGX Linux aarch64 / Python 3.12.3 对 `8ec76c8` 执行 `bash scripts/acceptance.sh`，201 项通过，0 失败、0 错误、0 跳过。 `test_live_media.py` 覆盖双向图片和文件、错房间与未授权人员、未知回执重启不重发、客户冒用审批和配置指令。源码 SHA256 为 `4161e78170cc8284910ce9dab35765797fb576622cecc49841b17088b698c0c5`。原始报告 `artifacts/dgx-media-20261126/acceptance.json`。 [对应 Mac 试点](../personal-account.md#2026-11-26-双向附件与-telegram-客户指令防护)

## 2026-11-26 团队部署与接入复验

DGX Linux aarch64 / Python 3.12.3 对 `8d596ab` 执行 `bash scripts/acceptance.sh`，227 项通过，0 失败、0 错误、0 跳过。覆盖团队初始化、应用扫码协议、Telegram 配对隔离、飞书定向与管理员审批邀请、客服交接轮转、安装器预检和失败回滚。源码 SHA256 为 `da481940814ef5e6b60f5e1f3470c92bff3dff6ac5c8f8a3f0220546c6860365`。报告及日志位于 `artifacts/dgx-onboarding-20261126/`。首轮 `cb10485` 的 226 项中，管理默认设置字段兼容断言失败，原始结果保存在同目录的 `initial/`。

## 2026-11-26 多公司实例复验

DGX Linux aarch64 / Python 3.12.3 对 `0910eb6` 执行 `bash scripts/acceptance.sh`，231 项通过，0 失败、0 错误、0 跳过。`test_company_accounts.py` 覆盖三家公司的独立业务连接、客户群与回复，以及重复配置、私有配置权限和本机管理页访问。源码 SHA256 为 `1051b804e67bdd0c8700c64d9733cd0853a938cd5da10f6ada65b4eccb7bd6b6`。报告及日志位于 `artifacts/dgx-companies-20261126/`。 [部署说明](../personal-deployment.md#多公司和多个-telegram-原账号)
