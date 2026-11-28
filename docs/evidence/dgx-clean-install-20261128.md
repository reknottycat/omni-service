# 全新 Linux 依赖安装验证（2026-11-28）

## 基线与范围

- 最新发行代码基线：`codex/public-release@feca72191d3bfb0ef5eeca8376e594e18a0a26bb`。
  [PR #8](https://github.com/reknottycat/omni-service/pull/8) 已合并；截至本次查询没有正式 GitHub Release 发行物。
- 本轮只解决 TDLib、飞书 SDK 与 lark-cli 的固定版本、摘要、打包、全新安装及服务解释器。
  原有 Telegram / 飞书扫码、账号确认、缺失权限申请与平台回执流程保留。
- 真实账号授权、飞书组织发版/审批、事件长连接、客户收发和 DGX 硬件均不计入本次通过项。
  没有挂载或修改既有部署，没有运行真实 systemd 启停。

## 固定版本核验

完整上游引用、14 个包的官方 PyPI 元数据和两种架构的实际 wheel SHA256 见
[依赖清单](dependency-inventory-20261128.json)。现有锁版本全部存在并符合官方摘要，未为追新升级。

| 组件 | 身份 |
|---|---|
| TDLib | `1.8.71`，commit `1b9efe84d6e9a2e2f6eadeeea32b0726d2b8c25d` |
| TDLib 源码 SHA256 | `e7d319970e62bfb48dbccc0a6b0d9a14361a0df04866332f5b6e1db1ca1a28c2` |
| 飞书 SDK | `lark-oapi==1.9.0`，共 14 个固定运行依赖 |
| Python lock SHA256 | `14033db46be611048f7f0169c9e9d4fa98eafa168c2f35408ed0970c498fe9d0` |
| CLI | `lark-cli 1.0.99`，Linux amd64 与 arm64 官方归档摘要均与代码固定值一致 |

两套官方 wheelhouse 均已实际下载并按锁校验；pip 的离线解析分别验证 x86_64 和 aarch64 目标。
将 ARM64 wheelhouse 交给 x86_64 目标解析时按预期失败。ARM64 wheel 可取得和可解析
不代表 ARM64 原生代码已执行。

## 完整回归

```bash
bash scripts/acceptance.sh
```

2026-11-28T02:23:09Z，Linux x86_64 / CPython 3.12.14：**304 项通过，0 失败、0 错误、0 跳过**。
测试前后源码指纹一致：

```text
e1e4e190064ef890b4dca01da340391252e48bfacf19f4e195cd8c0c90cb6250
```

[机器可读回归记录](clean-install-regression-20261128.json) 逐项保存用例、耗时、环境与文件 SHA256。
记录中的 `source_commit` 是补丁工作树所基于的发行提交；上述源码清单绑定实际被测修改，
不将其误说成未经修改的基线结果。

新增 50 项针对本任务的回归：CLI 13 项、SDK 10 项、bundle 20 项、setup 增加 4 项、
安装器增加 3 项。覆盖损坏归档、错误平台/版本、文件/元数据篡改、缺包、目录与链接、
路径穿越、归档重复项、输入替换竞态、残缺缓存、原子发布、私有解释器与保留已有服务。
其中合成 wheel 和 native fixture 只验证安装器行为，不能代替下面的真实官方组件测试。

## 真实组件与隔离环境

首次真实 SDK 检查在临时私有 venv 中离线安装全部官方 wheels；`pip check`、
`lark_oapi.aregister_app` 导入与重复完整性校验成功。以服务形式执行
`-P -B -m omni_service.onboarding --help` 后收据仍一致。
真实官方 Linux amd64 CLI 的 `--version` 返回 `lark-cli version 1.0.99`，重跑保留二进制 inode 与 mtime。

完整系统测试使用独立 Ubuntu 24.04.5 amd64 rootfs，源码只读挂载、独立非 root UID 1000、
独立配置/数据路径。官方 rootfs：

```text
https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/ubuntu-base-24.04.5-base-amd64.tar.gz
SHA256 e77b6f10c2590cef872b33ee9f635a0e3fd1f57fb074c0e52b5c7f56147a0c86
```

Python 3.12.3、CMake 3.28.3、Clang 18、OpenSSL 3.0.13 与 zlib 系统包仅安装到该隔离 rootfs。
系统先决条件由测试容器构建阶段准备；这不表示已在 DGX 裸机执行过安装器的 sudo/apt 分支。
断网阶段使用独立 network namespace，连接 `1.1.1.1:443` 返回 `ENETUNREACH`。

完整源码包安装与无编译器原生包验证的最终结果将在本节下方登记；进行中的构建不记为通过。

## 运行门禁与未验证范围

- 仓库交付 amd64 / arm64 两个原生 CI job，构建镜像和仅含系统运行库的 runtime 镜像分开。
  工作流配置、当地隔离验证、远端 CI 成功和 DGX 真机通过是四种不同状态。
- ARM64 目前只有官方分发文件/锁与解析验证；原生执行须由 ARM64 runner 或 DGX 独立取得结果。
- 真实飞书应用创建、扫码/管理员确认、授权回跳、组织审批和 Telegram 账号授权保持人工确认环节。
  无真实账号和平台权限测试，不声称真实收发通过。
- 现有部署、代理、账号、数据库、服务启动状态不变。

复验入口及安装命令见[全新 DGX 安装说明](../dgx-clean-install.md)。
