# OmniService 工作追踪

## 当前目标

**P1 · 个人账号可运行桥接开发已完成。** 2026-11-26 统一 Linux 验收为 **187 passed / 0 failures / 0 errors / 0 skipped**，包含 P0 回归和 P1 新增；源码摘要 `0f8a08b099c99b48e8def24f2fd55bf8b40653a58495a380889f8a9f58f0e969`。真实账号和 M6/DGX 尚未联调，继续由 Issue #3 跟踪。以下 P0 记录保留为历史基线。

| ID / 状态 / 负责人 | 目标及交付 | 依赖 | 复验及可观察结果 |
|---|---|---|---|
| P1-A / done / personal_core | 原账号身份、白名单、权限、持久游标、接管和撤回 | P1 契约 | `test_personal.py`、`test_personal_service.py`：真实 loopback 请求、重启不重复、撤回优先取消 |
| P1-B / done / platform_research、root | Telegram HTTP、飞书 REST/SDK、运行器和管理 API | P1-A；公开合同 | 客户端及运行器测试；SDK 独立检查 ACK 200/200/500 |
| P1-C / done / local_models | M6/DGX 客户端、三种协议和物流知识库 | 模型接口合同 | 模型/知识库测试：实际 HTTP、批准来源及失败转人工 |
| P1-D / done（调研/显式禁用）/ platform_research | WhatsApp 私人账号与官方 Business 共存差异 | 官方文档 | 真实私人号仍 blocked；不冒称已接通 |
| P1-E / done（调研/显式禁用）/ platform_research | MAX 当前 Open Client API 条件 | 平台公开文件 | 个人号 live 配置禁止启用，等待允许的路线 |
| P1-F / done / root、p1_independent_review | 演示、复核、缺陷修复与统一证据 | P1-A/B/C | 187 PASS；演示 9 项通过；3 项新增独立复现均修复 |
| P1-G / done / root | 配置、员工 ID 工具、CLI、备份、部署模板和资料清单 | P1-A/B/C | 配置、准备工具、管理 HTTP 测试；见 `docs/setup.md` |
| LIVE-01 / blocked / 账号持有人与联调负责人 | Telegram/飞书真实测试、M6/DGX 质量 | 本机授权、应用权限、端点 | 按 setup 真实清单取证；不影响开发验收 |
| LIVE-02 / blocked / 账号持有人与平台 | WhatsApp 原号路线、MAX 允许的接口 | 平台条件与路线 | 不以 Bot 模拟冒充原号连接 |

[当前证据](evidence/personal-acceptance.md) · [P1 PR #4](https://github.com/reknottycat/omni-service/pull/4) · [P1 Issue #3](https://github.com/reknottycat/omni-service/issues/3) · [用户准备](setup.md)。开发完成、CI、合并及真实联调分别标记。

**P0 · Linux 三渠道模拟闭环。** 以 Telegram、俄罗斯 MAX、WhatsApp 为客户入口，飞书为 7 人团队工作端，完成自动翻译、AI/人工协作、撤回及可靠消息路由。完成标准见 [验收合同](acceptance.md)。真实账号、完整媒体、Chatwoot/NextBridge 生产联调与真实模型质量作为后续增量。

当前记录更新于 2026-11-26。**P0 Linux 模拟验收已完成：87 项测试通过，15 项必选 AC 全部通过。** 源码提交 `75a2cd3`，交付 [PR #2](https://github.com/reknottycat/omni-service/pull/2) 已开启为 ready for review，尚未合并。远端 CI 未通过，作为独立阻塞记录。

状态为 `todo`、`in_progress`、`verify`、`done`、`blocked`。只允许有证据的任务进入 `done`。一个任务阻塞时，继续执行不依赖它的任务。

## 任务块

| 完成 | ID / 状态 / 负责人 | 对目标的贡献与交付物 | 依赖 | 复验命令 | 可观察的完成判据 / 证据 |
|---|---|---|---|---|---|
| [x] | T-01 / done / 协调负责人 | 冻结本轮范围与恢复入口；交付 `AGENTS.md`、验收合同、工作流、进度与 Issue/PR 模板 | 用户已明确的本轮目标 | `bash scripts/acceptance.sh` 后核对合同与报告 | AC ID、脚本结果与证据对应；后续需求未丢失；当前记录未虚报通过 |
| [x] | T-02 / done / 核心实现负责人 | 建立不会串客户的统一会话与消息模型；交付 SQLite 存储、队列和公共 HTTP 服务 | T-01 | `bash scripts/acceptance.sh` | AC-01、AC-03、AC-08、AC-09；重启后回查与多账号隔离证据 |
| [x] | T-03 / done / 渠道实现负责人 | 三个官方入口统一进入服务；交付 Telegram/MAX/WhatsApp 适配器、契约样例及本地假平台 | T-01；公共数据契约 | `bash scripts/acceptance.sh` | AC-02、AC-08、AC-13、AC-14；假平台记录真实 HTTP 请求 |
| [x] | T-04 / done / 会话实现负责人 | 员工留在飞书即可服务客户；交付会话建群/绑定、成员权限、自动语言与回复路由 | T-02、T-03 | `bash scripts/acceptance.sh`；`python scripts/demo.py` | AC-04、AC-05；普通成员消息自动译后对客、非成员无外发 |
| [x] | T-05 / done / 会话实现负责人 | 日常咨询自动接待，复杂咨询协同处理；交付模拟 AI、转人工与暂停/恢复状态 | T-02、T-04 | `bash scripts/acceptance.sh` | AC-06、AC-07；不重复建群、员工介入不抢答 |
| [x] | T-06 / done / 可靠性实现负责人 | 员工能知道消息是否真实发出/撤回；交付撤回状态、错误分类、有限重试、不确定结果处理 | T-02、T-03 | `bash scripts/acceptance.sh` | AC-10、AC-11、AC-12；正确消息 ID、无假成功与盲重发 |
| [x] | T-07 / done / 验证负责人 | 让用户一条命令重复验证；交付验收脚本、HTTP 演示、输入校验与结果 JSON | T-02 至 T-06 | `bash scripts/acceptance.sh`；`python scripts/demo.py` | AC-01 至 AC-15；`artifacts/acceptance.json` 与假平台可观察记录 |
| [x] | T-08 / done / 独立审查人与协调负责人 | 使本轮结果可核对并收口；交付独立复核、证据归档、GitHub 总控与 ready-for-review PR | T-07 | 重跑同一验收命令并核对代码/日志 | 必选 AC 全通过；证据绑定被测版本；PR、CI、模拟/真实验收分别报告 |

依赖说明：T-02 与 T-03 在公共数据契约固定后可并行；T-06 可与 T-04/T-05 在不共享写入文件的前提下并行。协调负责人负责共享文件、决策日志与最终集成。实际分工改变时更新本表，不允许多个代理同时覆盖同一文件。

## 验收状态索引

| 验收 ID | 状态 | 证据 |
|---|---|---|
| AC-01 | done | [87 项原始结果及逐项回链](evidence/linux-mock-acceptance.md#逐项验收回链) |
| AC-02 | done | [87 项原始结果及逐项回链](evidence/linux-mock-acceptance.md#逐项验收回链) |
| AC-03 | done | [87 项原始结果及逐项回链](evidence/linux-mock-acceptance.md#逐项验收回链) |
| AC-04 | done | [87 项原始结果及逐项回链](evidence/linux-mock-acceptance.md#逐项验收回链) |
| AC-05 | done | [87 项原始结果及逐项回链](evidence/linux-mock-acceptance.md#逐项验收回链) |
| AC-06 | done | [87 项原始结果及逐项回链](evidence/linux-mock-acceptance.md#逐项验收回链) |
| AC-07 | done | [87 项原始结果及逐项回链](evidence/linux-mock-acceptance.md#逐项验收回链) |
| AC-08 | done | [87 项原始结果及逐项回链](evidence/linux-mock-acceptance.md#逐项验收回链) |
| AC-09 | done | [87 项原始结果及逐项回链](evidence/linux-mock-acceptance.md#逐项验收回链) |
| AC-10 | done | [87 项原始结果及逐项回链](evidence/linux-mock-acceptance.md#逐项验收回链) |
| AC-11 | done | [87 项原始结果及逐项回链](evidence/linux-mock-acceptance.md#逐项验收回链) |
| AC-12 | done | [87 项原始结果及逐项回链](evidence/linux-mock-acceptance.md#逐项验收回链) |
| AC-13 | done | [87 项原始结果及逐项回链](evidence/linux-mock-acceptance.md#逐项验收回链) |
| AC-14 | done | [87 项原始结果及逐项回链](evidence/linux-mock-acceptance.md#逐项验收回链) |
| AC-15 | done | [87 项原始结果及逐项回链](evidence/linux-mock-acceptance.md#逐项验收回链) |

## 阻塞记录

此处只登记有证据的阻塞。缺少生产账号或真实模型不构成本轮阻塞。

| ID | 所属任务 / 负责人 | 原因与复现 | 解除条件 | 不受影响的下一步 | 状态 |
|---|---|---|---|---|---|
| CI-01 | 后续 CI 运行环境 / 协调负责人 | [首次远端运行](https://github.com/reknottycat/omni-service/actions/runs/45745662203) 在任何 step 前失败；`steps=[]`，runner 字段为空，日志 404；注释读取能力受限，根因未确认 | 获得具体运行错误原因并恢复 runner，使同一脚本实际执行；不擅自付费或更改账户权限 | P0 Linux 模拟验收、证据归档和 PR 交付已完成 | blocked；非 P0 门禁 |

新增阻塞时填写具体命令/输入、观察结果和证据路径。解除后保留历史，记录修复提交与复验结论。

## 证据与交接入口

- [验收合同](acceptance.md)：为什么做、什么算完成。
- [工作流程](workflow.md)：怎么拆、怎么验证、怎么恢复。
- [决策日志](decisions.tsv)：为什么采用某方案、何时变更、根据什么证据。
- [Linux 模拟验收证据](evidence/linux-mock-acceptance.md)：实际被测版本、命令和结果，运行后生成。
- `artifacts/acceptance.json`：机器可读结果，运行后生成。
- GitHub 总控：[Issue #1](https://github.com/reknottycat/omni-service/issues/1)，按 Linux 模拟验收范围收口。
- 交付分支：`feature/linux-mock-mvp`；[PR #2](https://github.com/reknottycat/omni-service/pull/2)，ready for review，未合并。
- CI：[运行列表](https://github.com/reknottycat/omni-service/actions)，目前未通过；不要把本地成功报告成远端 CI 成功。
- 已独立修复三项缺陷：[复核记录](evidence/independent-review.md)。

## 后续范围

完整需求待办以 [验收合同 B-01 至 B-10](acceptance.md#模拟边界与完整产品待办) 为准。P0 完成后保留这些条目为后续范围；没有新授权目标时，不为它们循环重跑已完成的 P0。
