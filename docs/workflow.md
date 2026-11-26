# 开发、验收与工作追踪

## 工作方式

本仓库参考 Cursor 官方 **pstack** 的 Feature 工作流程、可验证任务块和 Show me your work 决策轨迹。用户所说的“pstask”按前述 pstack 使用记录理解。本轮是已授权的实现与验收任务，使用该流程推进工作，不套用仅交付计划后停止的 Multi-phase plan 模式。

本仓库没有冒称安装或运行 pstack 原生插件。当前运行环境的独立代理复核可以用于分工和审查，但可用模型不具备 pstack 默认跨厂商 panel 的完整组合，不把同厂商独立复核写成跨厂商验证。

**用户明确的验收标准优先：本轮 Linux 模拟数据通过即可完成。** 不额外要求生产部署、真实客户消息、真实模型或跨厂商复核作为门禁。用户已授权的可逆工作直接推进；没有授权的真实客服发送不能以测试名义执行。

## 一个任务的完整闭环

P1 增量沿用同一流程，用户最新指令为“把能开发的都开发掉”。当前阶段由 [Issue #3](https://github.com/reknottycat/omni-service/issues/3) / [PR #4](https://github.com/reknottycat/omni-service/pull/4) 追踪：不依赖真实凭据的可运行开发与 Linux 模拟验收先完成；真实账号、模型质量、平台接入条件继续作为独立联调项。P0 的完成记录不撤销，P1 也不借用 P0 通过数冒充新版本。

1. **读目标。** 从 `AGENTS.md`、`docs/acceptance.md`、`docs/progress.md` 和 `docs/decisions.tsv` 恢复状态。检查当前分支、未提交修改与既有证据。
2. **冻结可观察结果。** 每块写清目标贡献、交付物、文件范围、依赖、运行命令和通过条件。先定义数据结构、身份键与状态，不靠多个布尔量拼出生命周期。
3. **拆出独立工作。** 先处理阻塞契约，再并行不共享文件的适配器、核心或验证工作。共享文件由一名负责人写入；集成边界顺序执行。
4. **实现最小完整行为。** 修改足以完成本块的代码和调用方，避免只添加未接线的空接口。需要工具证明的行为交付可复跑脚本。
5. **通过公共入口验证。** 以实际 HTTP 请求、SQLite 读回和假平台收到的请求验证行为。不能用内部函数被调用、编译成功或代理自述替代。
6. **失败即定位并回归。** 记录失败输入和原因，修复后复验。只对实际残余风险增加验证；已经满足合同后不扩展无关测试。
7. **记录与提交。** 每个完成块记录被测版本、命令、结果和证据路径。提交前检查 diff；进度与验收勾选必须依据结果。
8. **独立审查并交付。** 复核代码与证据是否支撑本轮用户行为，修复有效问题。创建 ready-for-review PR，并将总控 Issue、提交、CI 与证据链接起来。
9. **满足目标即收口。** 本轮必选条件全部满足后记 `done`。保留后续范围，不把真实接入要求倒灌为本轮未完成，也不重复派发已完成工作。

## 四项并行检查

每阶段开始或职责改变时重新核对：

| 检查项 | 本项目应用 |
|---|---|
| 前置阻塞 | 统一事件、账号/客户身份、消息映射、撤回状态先定契约 |
| 独立任务 | 渠道适配器、核心存储、文档/验证可以在不冲突的文件范围内并行 |
| 共享状态 | 共享模型、集成代码、追踪表和决策日志指定单一写入人；不同代理不得相互覆盖 |
| 最小安全拆分 | 高耦合逻辑由单一负责人持有；不为增加代理数量拆出大量同步成本 |

## 任务状态与阻塞

| 状态 | 含义 | 允许的下一步 |
|---|---|---|
| `todo` | 有目标但未开始 | 明确负责人后进入 `in_progress` |
| `in_progress` | 正在实现或补齐交付物 | 具备验证入口后进入 `verify`；有真实障碍进入 `blocked` |
| `verify` | 实现已可运行，正在核对验收 | 通过进入 `done`；失败返回实现或登记阻塞 |
| `blocked` | 有实际障碍，无法推进该块 | 写明负责人、原因、复现、证据、解除条件；继续其他可做任务 |
| `done` | 合同范围内已有可复验结果 | 停止重复派发；新需求/已确认回归另建增量 |

不得以“等用户确认”替代已经获得授权的日常实现判断。也不得因为某个生产凭据尚不存在，就把本轮模拟验证标为阻塞。需要权限或外部条件时，先完成不依赖它的工作，再报告具体剩余项。

## 证据契约

每次验收至少保留：

- 被测提交 SHA，或提交前的基线 SHA 与工作树状态；不能把其他版本的通过记录移用到当前版本。
- 操作系统、Python 版本及必要依赖版本。
- 实际执行命令与退出码。
- 输入场景、预期结果、实际可观察结果及其对应 AC ID。
- 原始报告/日志路径、失败修复与未验证边界。
- 模拟平台与模拟模型标识，实际调用范围和本轮数据规模。

`scripts/acceptance.sh` 是复验入口；`artifacts/acceptance.json` 供机器核对，`docs/evidence/linux-mock-acceptance.md` 供人查看。最终证据应链接当前版本相关 CI；尚无 CI 结果时明确记录，不把本地通过写成 CI 通过。

平台能力差异由状态表示。尤其 WhatsApp 不能因为验收使用假服务就虚构已发送消息的全员撤回 API。超时后的投递结果若无法核对，保留 `unknown`，不将潜在重复消息当作可靠性修复。

## 决策轨迹

统一使用 `docs/decisions.tsv`，只追加，由协调负责人写入。列沿用 pstack：

```text
ts	phase	decision	why	evidence	result
```

记录选型、关键范围判断、完成检查点、失败返工和阻塞，不记录所有工具调用。证据列使用可回查路径/链接/SHA；更正错误时新增一行说明替代关系，不擦除历史。写入单元格时处理制表符、换行与表格公式前缀。新执行会话先读已有日志并追加 `start` 记录，指明此前已读的记录范围和当前负责人。

最终核对日志是否与真实操作一致；没有证据的结论保持未验证。当前环境无法做默认跨厂商审查时，写明实际复核方式及局限。

## GitHub 交付和恢复

- 以一个总控 Issue 关联本阶段合同、任务与 PR。对现有事项追加证据，不为同一任务重复建 Issue。
- PR 使用 `type(scope): subject` 标题，ready for review；正文说明为什么做、改了什么、范围、取舍、影响与验证，长日志放文件并链接。
- 本轮实现授权不自动等于合并授权。没有用户明确的合并指令时不自动 merge，也不修改仓库保护策略。
- Linux 模拟验收、CI、PR 合并及真实环境验证分别标记，分别给证据。
- 中断恢复先看进度和最后的决策/证据，复用通过结果；只重跑受新修改影响的检查或合同要求的总入口。
- 不提交真实 token、账号私密信息、真实客户会话或运行数据库。测试 fixtures 必须是明确的模拟数据。

## 官方流程依据

2026-11-26 阅读以下原文后采用相关流程；不创建或修改技能文件。

- [poteto-mode](https://github.com/cursor/plugins/blob/main/pstack/skills/poteto-mode/SKILL.md)：以目标开头，先设计再分工，审查自己负责的结果。
- [worker](https://github.com/cursor/plugins/blob/main/pstack/skills/poteto-mode/worker.md)：用户给定的完成标准就是边界，不增加额外门禁。
- [Feature](https://github.com/cursor/plugins/blob/main/pstack/skills/poteto-mode/playbooks/feature.md)：阻塞契约、独立工作、共享状态、最小安全拆分。
- [Sequence work into verifiable units](https://github.com/cursor/plugins/blob/main/pstack/skills/principle-sequence-verifiable-units/SKILL.md)：一块实现、一块验证，按可证明的顺序交付。
- [Prove it works](https://github.com/cursor/plugins/blob/main/pstack/skills/principle-prove-it-works/SKILL.md)：检查真实产物和运行结果。
- [Test behavior, not implementation](https://github.com/cursor/plugins/blob/main/pstack/skills/principle-test-behavior-not-implementation/SKILL.md)：验证公开接口可观察的行为。
- [Build the lever](https://github.com/cursor/plugins/blob/main/pstack/skills/principle-build-the-lever/SKILL.md)：交付可复验的最小脚本。
- [Show me your work](https://github.com/cursor/plugins/blob/main/pstack/skills/show-me-your-work/SKILL.md)：单一追加式决策日志，证据回链。
