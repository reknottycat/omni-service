# 安装与分发补丁独立复核

范围：相对 `feca72191d3bfb0ef5eeca8376e594e18a0a26bb` 的新安装入口、原生依赖、SDK、CLI、
bundle、服务解释器以及干净环境脚本/CI。复核通过独立子任务完成，不宣称跨厂商审查。

## 发现与处理

| 具体风险 | 已落实的修复 | 可观察证据 |
|---|---|---|
| 服务仍可能使用无 SDK 的系统 Python | 首次 unit 在依赖校验后生成，部署选择收据验证过的私有 Python | 最小 runtime 检查 ExecStart 指向 SDK；安装器回归 |
| 以 unit 前缀认定可覆盖草稿，会覆盖用户自定义内容 | 取消刷新已有 unit；首阶段只准备配置，不生成 unit | 自定义草稿与已启用实例的保留测试 |
| PR #8 旧 SDK 命名导致 prepare 前置检查死循环 | `--defer-unit --no-start` 不选择 SDK，准备完成后的第二阶段再验证 | 两阶段调用契约与旧缓存保留路径审查 |
| Bundle hash 后重新打开原始路径可被替换 | 先固定私有快照并计算外层摘要，只解该快照 | 在 tar 打开前替换原输入仍只获得受信内容的回归 |
| Manifest 自报 hash 不能代替官方 pin | 实际 TD source、CLI、lock 和 wheels 独立比对内置 pin | 重新计算清单后替换依赖仍拒绝 |
| 每个 wheel 都合法，但包集合不完整或架构错误 | 要求锁内每项都有 wheel；复用 wheelhouse 时离线解析目标兼容性 | 缺一包拒绝；ARM64 wheels 在 x86_64 目标拒绝 |
| 中断可能留下半个 TDLib 安装 | 库和收据在暂存目录中完成，再整体 rename 发布 | 加载失败无已发布目录；重跑保持库内容与 mtime |
| 带编译器的 offline 镜像可能掩盖源码 fallback | runtime 镜像移除所有编译工具，并在测试中断言不存在 | 独立 runtime + 断网 native 安装 |

复核未发现支持范围内尚需阻断验证的具体安装失败、旧部署覆盖或摘要绕过问题。
该结论不能替代实际运行；各项运行结果以[验证记录](dgx-clean-install-20261128.md)为准。

## 必须保留的验收边界

- Ubuntu amd64 隔离实测、ARM64 官方文件检查、ARM64 CI 运行、DGX 真机验收分别记录。
- 测试系统包由 Dockerfile / 独立 rootfs 的 apt 阶段准备；尚未在 DGX 裸机执行
  `install-dgx.sh --install-system-deps` 的 sudo/apt 分支。
- SDK fixture、无效 native fixture 与模拟平台只验证失败/路由合同；官方组件的实际安装和加载另有证据。
- 准备组件不等于账号授权，也不等于飞书发版、事件连接、客户消息或服务启动成功。
