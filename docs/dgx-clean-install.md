# 全新 DGX / Ubuntu 24.04 安装

本文针对全新 Linux ARM64（DGX Spark）或 amd64 实例。基线为发行分支
`codex/public-release` 的 `feca72191d3bfb0ef5eeca8376e594e18a0a26bb`，包含已合并的
[PR #8](https://github.com/reknottycat/omni-service/pull/8) 和之后的群名配置变更。
[11 月 27 日记录](evidence/managed-onboarding-20261127.md) 的真实授权与真机边界继续有效。

## 安装与授权的分界

`install-dgx.sh` 只为新团队准备私有配置、依赖和未启动的 systemd 用户服务文件。
发现同名配置目录、数据目录或服务文件即拒绝；不写入既有实例。它不登录飞书或 Telegram，
不启用/重启 systemd，不建立聊天数据库，不修改现有代理。源码目录可以只读挂载。

账号配置仍使用原有 `setup-personal.sh TEAM configure -- ...`：飞书官方应用创建与身份扫码、
缺失权限申请二维码、管理员审批，以及 Telegram 官方 TDLib 扫码、验证码/二步验证和配对回执。
本次没有改写这些授权协议，也没有替用户批准真实账号权限。

## 1. 从源码自动准备新实例

在普通服务用户下执行，**不要 sudo 整个安装器**。以下团队名须尚未使用：

```bash
bash scripts/install-dgx.sh dgx-new --install-system-deps
```

系统支持范围为 Ubuntu 24.04、CPython 3.12、原生 `aarch64` 或 `x86_64`。
`--install-system-deps` 只对 Ubuntu 系统包调用 `sudo apt-get`，安装 Python/venv、CA 证书、
Clang/C++、CMake、gperf、OpenSSL 和 zlib 开发依赖。已具备系统包时省去该参数。
源码编译默认 2 个任务，可用 `--jobs 1` 降低峰值内存；只从固定 TDLib 提交构建。
系统包通过 Ubuntu 的签名仓库取得；其安全更新版本不冒充逐字节固定的编译工具链。

成功后保持 `mode=disabled`。路径均在当前用户的 XDG 配置/数据目录：

| 内容 | 默认位置 |
|---|---|
| 实例配置与私有凭据 | `~/.config/omni-service/dgx-new/` |
| CLI、SDK 和原生库 | `~/.config/omni-service/dgx-new/tools/` |
| 服务代码副本 | `~/.local/share/omni-service/dgx-new/releases/` |
| 未启动的用户服务文件 | `~/.config/systemd/user/omni-service-dgx-new.service` |

准备完成后按[原有首次接入说明](personal-deployment.md#2-从命令行完成首次接入)执行
`bash scripts/setup-personal.sh dgx-new configure -- ...`。真实账号授权、飞书组织发布和事件设置完成后，
再由部署者执行 `bash scripts/install-personal.sh dgx-new` 做平台预检及启动。

如果首次准备因下载或构建中断，保留该未启用实例，并用
`bash scripts/setup-personal.sh dgx-new prepare` 重试；新安装入口仍会拒绝同名实例。
已存在但完整性不匹配的缓存会报告失败并保留，不自动删除、覆盖或降级到 PATH 中的其他工具。
残缺 SDK 目录需先由部署者核对报错路径后移出该新实例，不能据此删除既有部署目录。

## 2. 完整依赖包：不在 DGX 上下载和编译组件

在 **Ubuntu 24.04、同一目标架构、CPython 3.12** 上构建原生包：

```bash
python3.12 -I -B scripts/package-onboarding.py \
  --output /absolute/output/omni-onboarding-linux-arm64.tar.gz
```

在 amd64 机器生成 amd64 包；不会把 amd64 文件标成 ARM64。打包器拒绝在其他发行版生成
Ubuntu 原生包，避免将较新 glibc 的构建误交给 DGX。已有本机 `prepare` 组件时可以加
`--tools /absolute/instance/tools`，它只取经过验证的 CLI 与原生库，不打包账号配置、私钥、
venv、日志或数据库。`--source-only` 可生成源码依赖包，安装端仍需上述编译工具。

包中包含：固定 TDLib 源码（含许可证）、可选的同架构 `libtdjson.so`、官方 CLI 原归档
（含许可证）、14 个固定 Python wheels、完整 hash lock、逐文件大小和 SHA256 清单。
原生包另记录构建系统。输出旁边的 `.sha256` 是完整压缩包摘要；必须通过可信的发布渠道取得，
内部清单不能自证真实性。摘要用于完整性与版本绑定，不等同于上游或维护者的数字签名。

目标新机器先准备 Python 3.12/venv、CA 证书、libstdc++ 和 zlib，然后使用发布时核对的摘要：

```bash
bash scripts/install-dgx.sh dgx-new \
  --bundle /absolute/omni-onboarding-linux-arm64.tar.gz \
  --bundle-sha256 '这里替换为从可信发布记录取得的64位SHA256'
```

需要自动装系统运行库时加 `--install-system-deps`；这一步访问 Ubuntu 仓库。
系统运行库已具备时，原生依赖包安装完全离线，目标机器不需要 CMake、编译器或全局 pip。
源码包离线安装还需 `--source-build` 及预先具备的编译工具。
安装器先固定输入归档的私有快照并校验外层摘要，再拒绝链接、设备、路径穿越、重复条目、
超限文件及清单偏差。TDLib 源码、CLI 归档和 wheels 还会与代码内的上游固定摘要独立复验。
任何一项不匹配，都不进入原生加载或账号授权。

## 3. 固定依赖及验证

| 组件 | 固定身份 | 安装/重用校验 |
|---|---|---|
| TDLib | `1.8.71` / commit `1b9efe84d6e9a2e2f6eadeeea32b0726d2b8c25d` | 官方源码 SHA256；原生包外层与内部 SHA256；本机 `ctypes` 加载、4 个 JSON API 导出、离线版本回读；库和收据原子发布 |
| 飞书 Python SDK | `lark-oapi==1.9.0`，完整依赖见 `requirements-onboarding.lock` | 全部 14 包版本和 wheel SHA256；`pip --require-hashes --only-binary=:all:`；`pip check`；官方注册函数导入；每次重用核对 venv 全量文件、RECORD、版本和解释器身份 |
| 飞书 CLI | `lark-cli 1.0.99` | 官方归档 SHA256；ELF/Mach-O 架构；隔离配置下执行 `--version`；现有二进制及收据与固定归档逐字节对应 |

SDK 从最终私有路径创建，避免搬迁 venv 后产生失效的 pip shebang。
首次 systemd 文件等 SDK 校验成功后才创建，执行文件指向私有 Python；后续准备不改任何既有
服务文件，包括人工修改的草稿。历史 PR #8 的 TDLib 平铺缓存按原收据验证后继续可用。

上游固定摘要：

```text
TDLib source: e7d319970e62bfb48dbccc0a6b0d9a14361a0df04866332f5b6e1db1ca1a28c2
lark linux-arm64: 2837145e72922d558538494d0da30ac6c509681fc0791f8818f4ddb87f5125b4
lark linux-amd64: fd29f51030dde6919b6ca9173bc87852d3846f253d198022ea2d9ee63527b945
```

来源：[TDLib 固定源码](https://github.com/tdlib/td/tree/1b9efe84d6e9a2e2f6eadeeea32b0726d2b8c25d)、
[CLI 官方 Release](https://github.com/larksuite/cli/releases/tag/v1.0.99)、
[SDK 官方 PyPI 元数据](https://pypi.org/pypi/lark-oapi/1.9.0/json)。

## 4. 干净环境复验

普通逻辑与负测：

```bash
bash scripts/acceptance.sh
```

原生 amd64 / ARM64 分别执行下面的容器测试；首次完整构建可能需要较长时间。
镜像使用固定 Ubuntu 多架构摘要，默认没有 OmniService、SDK、TDLib 或飞书 CLI。
源码只读挂载，组件在全新非 root 用户目录安装：

```bash
docker build -f deploy/clean-install.Dockerfile --target build -t omni-clean-build .
docker build -f deploy/clean-install.Dockerfile --target runtime -t omni-clean-runtime .
mkdir -p /absolute/omni-clean-result
sudo chown 10001:10001 /absolute/omni-clean-result
docker run --rm --cap-drop=ALL --security-opt=no-new-privileges \
  --mount "type=bind,src=$PWD,dst=/repo,readonly" \
  --mount type=bind,src=/absolute/omni-clean-result,dst=/work \
  omni-clean-build online /work
docker run --rm --network=none --cap-drop=ALL --security-opt=no-new-privileges \
  --mount "type=bind,src=$PWD,dst=/repo,readonly" \
  --mount type=bind,src=/absolute/omni-clean-result,dst=/work \
  omni-clean-runtime offline /work
```

测试检查真实组件加载、SDK 可导入、依赖无缺失、重跑不改变配置/凭据/服务/native 文件、
已有团队拒绝覆盖、篡改包被拒绝，以及没有授权配置、数据库或 systemctl 调用。
离线阶段必须关闭网络，不能以 mock 下载器代替离线结果。
CI 已配置原生 `ubuntu-24.04` 和 `ubuntu-24.04-arm` 两个 job，归档依赖包和限定范围的 JSON
证据，**不上传实例配置和凭据**。工作流存在不等于远端运行成功；以具体运行记录为准。

本轮实际执行环境、源码摘要、测试命令及尚未验证的 DGX / 真实账号范围见
[2026-11-28 验证记录](evidence/dgx-clean-install-20261128.md)。
