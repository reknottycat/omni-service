# OmniService 工作追踪

## 2026-11-28 全新 DGX 安装增量

基线 `feca72191d3bfb0ef5eeca8376e594e18a0a26bb`，关联既有 PR #8 与 Issue #1。
本轮不改账号扫码协议、不覆盖既有部署；新增工作独立于历史 P0 统计。

| ID / 状态 / 负责人 | 价值与交付 | 允许修改的文件 | 依赖 / 复验 / 通过条件 |
|---|---|---|---|
| DGX-01 / done / 依赖审查 | 核实三类上游固定版本和摘要，避免下载漂移 | `onboarding_tools.py`、对应专项测试 | 官方 Release/PyPI/source；固定值可逐项核对 |
| DGX-02 / done / SDK 与总控 | 完整依赖包、私有 SDK 校验、原生库原子发布、新安装脚本与正确解释器 | `deploy/`、安装/打包 scripts、依赖专项测试 | 完整回归 304 项通过；摘要错误不执行、不修改旧实例 |
| DGX-03 / done（限定 amd64 实测与 CI 配置）/ 干净环境验证 | 全新 Ubuntu amd64 原生组件与断网包安装证据；amd64/arm64 CI 配置 | `scripts/test-clean-onboarding.sh`、容器及工作流、证据 | 最新源码入口断网构建成功；定稿 native 包在无编译器 runtime 断网安装成功；重跑幂等、无授权/DB/systemctl |

具体命令见[全新 DGX 安装](dgx-clean-install.md)，结果见[2026-11-28 验证记录](evidence/dgx-clean-install-20261128.md)。
ARM64 原生执行、远端 CI、DGX 真机和真实账号授权保持待验，不包含在上述 done 范围内。

## 当前目标

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

## 客服群命名

[Issue #13](https://github.com/reknottycat/omni-service/issues/13) / verify / 协调负责人。管理页保存可选业务简称，新群标题采用客户名、业务简称、渠道和服务器本地日期。DGX 复验完毕，见[客服群命名复验](evidence/linux-mock-acceptance.md#2026-11-27-客服群命名复验)。
