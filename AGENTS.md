# OmniService 工作约定

## 目标和优先级

本仓库为用户的跨平台 AI 客服系统。当前阶段为 **P0 · Linux 三渠道模拟闭环**：Telegram、俄罗斯 MAX、WhatsApp 为客户入口；7 人团队使用飞书；自动识别客户语言并翻译员工普通消息；支持 AI/人工接管、持久会话、可靠路由和准确的撤回结果。

用户已明确：**本轮使用模拟数据在 Linux 上通过即可完成验收。** 真实账号、生产部署、真实模型、翻译质量、完整媒体或 Chatwoot/NextBridge 正式联调不是本轮必要门禁。不得虚称这些后续范围已完成。

用户的新指令优先于本文件与参考技能。请直接推进已授权的可逆工作，不反复要求确认。

## 开始与恢复

按需读 `docs/acceptance.md`、`docs/progress.md`、`docs/workflow.md` 及 `docs/decisions.tsv`。先检查当前分支和工作树，保留他人未提交工作。通过现有证据恢复，不重做已验收目标。

每块任务必须写清价值贡献、交付物、允许修改的文件、依赖、复验命令与可观察通过条件。状态使用 `todo`、`in_progress`、`verify`、`done`、`blocked`。阻塞须有负责人、原因、复现、证据和解除条件；其他无依赖工作继续。

## 产品不变量

- 服务群普通成员消息默认发给客户，不添加内部/外部消息双模式。
- 账号、渠道、客户/聊天身份共同决定路由，禁止串会话或未经确认合并跨渠道身份。
- 只有当前服务会话授权成员可以对客回复。
- 员工介入暂停当前会话 AI 自动回复，明确恢复后再启用。
- 模拟翻译/AI 必须显式标记；翻译失败不能静默以原文冒充目标语言成功。
- 撤回状态区分 `cancelled`（发送前取消）、`deleted`、`unsupported`、`failed`、`unknown`。平台没有能力时不得虚构成功。
- 对无法确定是否已投递的请求保留 `unknown`，不盲目重复发送。
- webhook 重放、批量事件、错误账号、成员权限、限流和重启按合同验证。

## 实现和验证

先冻结数据契约，再并行不共享文件的任务。共享文件和 `docs/decisions.tsv` 由协调负责人写入；子代理只能修改分配的文件。

完整复验为 `bash scripts/acceptance.sh`；HTTP 演示为 `python scripts/demo.py`。应真实运行 HTTP 入口、SQLite 队列与本地假平台。检查公开行为和外部假服务收到的请求，不能只检查 helper 调用或编译。

结果写入 `artifacts/acceptance.json`，归档到 `docs/evidence/linux-mock-acceptance.md`。证据绑定被测版本、环境、命令、结果和 AC ID。没有运行或结果不确定，不得勾选通过。只为具体风险或合同门禁增加测试，满足目标后收口。

Chatwoot/NextBridge 为计划中的底座；当前独立 adapter runtime 是 Linux 协议验收台，不宣称已完成两者联调。保留 `docs/acceptance.md` 中的后续产品待办。

## 追踪与交付

参考 pstack 官方流程，入口与来源见 `docs/workflow.md`。不宣称运行了原生插件或当前环境不具备的跨厂商 panel。

用单一总控 Issue 关联任务、PR 和证据；不要重复建追踪项。逐任务更新进度和追加式决策日志。PR ready for review，不开草稿；标题 `type(scope): subject`，正文不超过 40 行，长证据用链接。除非用户明确授权，不自动合并、部署、发送真实客户消息或修改分支保护。

本轮 mock 测试只使用模拟数据与本地假服务。不要提交 token、真实客户信息、运行数据库或敏感日志。验收、CI、合并和真实接入状态分别报告。
