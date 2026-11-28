@AGENTS.md

## Claude Code 验证约定

交付前用项目 `/verify` 或依 `docs/workflow.md` 直接运行对应仓库命令，验证公开使用入口。不要把编译、helper 调用或 mock 成功描述成真实平台接通。没有授权、设备或真实数据时，执行本轮允许的替代检查，明确记录未验证的真实边界。

保持实现可读，不补流水账式备注、复述代码的注释或无关总结。必要的边界说明、原因、安全约束、验收证据和公共接口文档应保留。

审查和跟进不依赖特定插件。优先复用 Alibaba OpenCodeReview 和 pstack；未安装时由独立审查者按目标分支基线复核，用 GitHub CLI 查 PR 和 CI，具体见 `docs/workflow.md`。目标分支没有 `REVIEW.md` 时用同一提交的 `AGENTS.md`，两个都没有才用固定的默认审查规则。
