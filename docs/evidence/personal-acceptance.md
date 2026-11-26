# P1 Linux 开发验收

**结果：187 项通过，0 失败、0 错误、0 跳过。** 统一执行时间约 51 秒。P0 回归与 P1 个人账号增量在同一源码版本上运行；另执行完整个人账号演示及真实安装的官方飞书 SDK 离线检查。

## 版本与原始证据

- 原始被测基线：`e70497abdd1c917d8facf5557cc8fa84156d2f9c`，运行时有本轮未提交改动，原始 JSON 如实保留 `worktree_dirty=true`。
- 被测代码、测试、脚本、fixture、配置、部署模板、依赖清单及 workflow 的总摘要：`0f8a08b099c99b48e8def24f2fd55bf8b40653a58495a380889f8a9f58f0e969`。
- 验收前后摘要一致：`source_consistent=true`。
- [187 项逐项结果和文件摘要](personal-local-run.json)
- [原始运行日志](personal-local-run.log)
- [个人账号 HTTP 演示](personal-demo.json)
- [官方 SDK 实际检查](personal-sdk-smoke.json)
- [独立审查与复验](personal-review.md)

源码提交：[`4f1e41ea7e1e0be133995c0b3d80e72b157239e7`](https://github.com/reknottycat/omni-service/commit/4f1e41ea7e1e0be133995c0b3d80e72b157239e7)。远端完整递归树中 **81 个文件**的 Git blob 均与本地字节一致，详见 [版本绑定证据](personal-source-verification.json)。运行器、测试、配置、依赖及脚本在统一验收后没有变化；随后的提交只归档文档与证据。不将提交前报告的基线字段改写成事后提交号。

## 实际环境和命令

Linux 6.18.57 x86_64，Python 3.12.14，SQLite 3.53.1。

```bash
bash scripts/acceptance.sh
python scripts/demo_personal.py
PYTHONPATH=/tmp/omni-live-sdk:src python tests/test_feishu_live.py --official-sdk-smoke
```

SDK 1.9.0 实际安装在独立测试目录 `/tmp/omni-live-sdk`，没有改用户主机。正式环境按 `requirements-live.txt` 安装后可直接用 `PYTHONPATH=src` 执行 SDK 检查。

## 结果分布

| 模块 | 通过数 | 证明的主要行为 |
|---|---:|---|
| P0 渠道、服务、语言、HTTP、回执、传输及演示 | 87 | 原有三渠道路由与 1000 个合成客户隔离回归 |
| 配置与准备工具 | 8 | 默认拒绝、密钥保护、员工主动准备 ID、实例锁和备份 |
| 飞书监听与平台 HTTP 客户端 | 21 | 长连接回调、启停、实际 HTTP 请求、Token 刷新、未知结果 |
| 本地模型与知识库 | 23 | 三种协议、语言检测、翻译失败、批准答案及来源 |
| 个人账号契约与持久服务 | 35 | 白名单、游标、7 员工、本人接管、重启、撤回及恢复 |
| 运行器与个人账号演示 | 7 | 原账号→飞书→模型→原账号的实际 HTTP 全链路 |
| 审查回归 | 6 | 账号绑定、撤回状态、未知结果恢复及有界 drain |
| **合计** | **187** | **全部通过** |

## 完整个人账号演示观察

假 Telegram、假飞书及假模型作为独立 HTTP 服务，调用方使用真实运行器和真实 HTTP 客户端。

- 客户发送俄语问候，通过原账号连接返回 AI 答案，并建立一个飞书群。
- 7 名员工中文回复先持久入队；关闭并重启服务后，7 条都恢复，正确翻译成俄语，由同一原账号发送给正确客户。
- 员工 1 的目标消息通过实际发送回执撤回；重复员工事件未重复发送。
- 不在白名单的私人消息样例没有出现在业务数据库。
- 恢复 AI 后，俄语单证问题返回批准的物流知识答案。
- 实际观察：9 条 Telegram 外发、1 次删除、1 个飞书群、5 条飞书消息、9 次模型 HTTP 请求。

## 官方 SDK 观察

使用真实 `lark-oapi==1.9.0` 事件模型和 protobuf，替换外部 WebSocket 网络；连接发现走实际 loopback HTTP。

```json
{"ack_codes":[200,200,500],"actual_sdk_dispatch":true,"actual_sdk_start_stop":true,"durable_events":2,"external_network_calls":0,"loopback_discovery_requests":1,"sdk_version":"1.9.0"}
```

两条事件先提交 SQLite 再成功 ACK；模拟存储失败时 SDK 返回失败 ACK，便于平台重投。这是实际 SDK 离线行为验证，不是真实飞书账号连接。

## 真实范围与剩余外部条件

- 开发验收、模拟平台、实际客户端和 SDK 离线验证：通过。
- 用户账号登录授权、飞书权限发布、M6/DGX 实际地址与模型质量：未执行，等本机资料准备。
- WhatsApp 普通私人账号路线与 MAX 平台允许条件：blocked，没有用 Bot 模拟代替。
- 媒体二进制中转、真实并发容量、Chatwoot/NextBridge 联调：本轮未实现或未验证。
- CI 和 PR 合并独立记录，不能用本地 PASS 代替远端 CI PASS。

验收条件回链见 [P1-AC-01 至 P1-AC-12](../acceptance.md#p1-增量验收原有个人账号与飞书)，运行与账号准备见 [setup](../setup.md)。
