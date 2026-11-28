"""Install a checkout as a private, per-team Linux systemd user service."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile


REPOSITORY = Path(__file__).resolve().parents[1]
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
MODES = {"personal-text-pilot", "personal-managed"}
AMBIENT_LARK_CREDENTIALS = (
    "LARKSUITE_CLI_APP_ID", "LARKSUITE_CLI_APP_SECRET",
    "LARKSUITE_CLI_USER_ACCESS_TOKEN", "LARKSUITE_CLI_TENANT_ACCESS_TOKEN",
    "LARKSUITE_CLI_TENANT_ACCESS_TOKEN_SOURCE", "LARKSUITE_CLI_AUTH_PROXY", "LARKSUITE_CLI_PROXY_KEY",
)


class InstallationError(Exception):
    pass


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise InstallationError(f"目录必须由当前用户持有，且仅本人可访问。{path}")


def private_bytes(path):
    # A link here could read a different team's saved credentials.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise InstallationError(f"配置文件必须为当前用户持有且仅本人可访问的普通文件。{path}")
        return file.read(512_001)


def atomic_write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".omni-", delete=False) as pending:
        stage = Path(pending.name)
        try:
            pending.write(data)
            pending.flush()
            os.fsync(pending.fileno())
            stage.replace(path)
        finally:
            stage.unlink(missing_ok=True)


def provision(path, incoming, initial):
    if path.exists() or path.is_symlink():
        saved = private_bytes(path)
        if incoming is not None and incoming != saved:
            raise InstallationError(f"已有配置与输入不同，已保留现有文件。{path}")
        return saved
    data = incoming if incoming is not None else initial
    atomic_write(path, data)
    return data


def package_sources(data_root):
    sources = REPOSITORY / "src" / "omni_service"
    files = {}
    for path in sorted(sources.rglob("*")):
        if path.suffix in {".py", ".html", ".css", ".js"} and "__pycache__" not in path.parts:
            if not path.is_file() or path.is_symlink():
                raise InstallationError(f"代码目录包含无法打包的文件。{path.relative_to(REPOSITORY)}")
            files[path.relative_to(sources)] = path.read_bytes()
    if Path("personal_runner.py") not in files:
        raise InstallationError("找不到 OmniService 服务源码，请从项目目录安装。")
    digest = hashlib.sha256()
    for path, content in files.items():
        digest.update(path.as_posix().encode() + b"\0" + hashlib.sha256(content).digest())
    releases = data_root / "releases"
    private_directory(releases)
    release = releases / digest.hexdigest()[:24]
    if release.exists():
        private_directory(release)
        return release
    with tempfile.TemporaryDirectory(dir=releases, prefix=".stage-") as temporary:
        staged = Path(temporary) / "release"
        package = staged / "src" / "omni_service"
        for path, content in files.items():
            target = package / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        staged.rename(release)
    return release


def quote(value):
    # systemd expands its percent specifiers even inside quotes.
    value = os.fspath(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise InstallationError("部署路径包含系统服务格式无法表示的控制字符。")
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def service_command(action, config, credentials, python=None):
    return (Path(python or sys.executable).absolute(), "-P", "-B", "-m", "omni_service.onboarding",
            action, "--config", config, "--secrets-file", credentials)


def private_python(config_root):
    """Select only a verified existing SDK; deployment never downloads implicitly."""
    tools = config_root / "tools"
    if not tools.exists() or not any(tools.glob("python-cpython-312-*")):
        return Path(sys.executable).absolute()
    sys.path.insert(0, str(REPOSITORY / "deploy"))
    from sdk_dependencies import find_sdk, SDKError
    try:
        python = find_sdk(tools)
    except SDKError as exc:
        raise InstallationError(str(exc)) from None
    if python is None:
        raise InstallationError("已安装 SDK 与本次代码的锁文件不匹配，请先准备本次版本的依赖。")
    return python


def unit_text(team, release, command, additions):
    environment = "\n".join(f"Environment={quote(key + '=' + value)}" for key, value in additions.items())
    command_line = " ".join(quote(arg) for arg in command)
    working_directory = str(release).replace("%", "%%")
    return f"""[Unit]
Description=OmniService bridge ({team})
StartLimitIntervalSec=90
StartLimitBurst=5

[Service]
Type=exec
WorkingDirectory={working_directory}
{environment}
UnsetEnvironment={" ".join(AMBIENT_LARK_CREDENTIALS)}
ExecStart=:{command_line}
UMask=0077
NoNewPrivileges=yes
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
""".encode()


def run_systemctl(*args):
    result = subprocess.run(("systemctl", "--user", *args), check=False)
    if result.returncode:
        raise InstallationError(f"systemd 用户服务操作失败（{args[0]}）。")


def activate_unit(unit, previous, proposed):
    changed = previous != proposed
    was_active = False
    if previous is not None:
        status = subprocess.run(("systemctl", "--user", "is-active", "--quiet", unit.name), check=False)
        if status.returncode not in (0, 3, 4):
            raise InstallationError("无法确认已有系统服务状态，已保留其配置。")
        was_active = status.returncode == 0
    if changed:
        atomic_write(unit, proposed)

    attempted_restart = False
    try:
        run_systemctl("daemon-reload")
        attempted_restart = True
        # Restart also loads updated settings or credentials when the unit is unchanged.
        run_systemctl("restart", unit.name)
        run_systemctl("is-active", "--quiet", unit.name)
        run_systemctl("enable", unit.name)
    except (OSError, InstallationError) as original:
        try:
            if previous is not None and changed:
                atomic_write(unit, previous)
                run_systemctl("daemon-reload")
            if attempted_restart:
                if was_active:
                    run_systemctl("restart", unit.name)
                    run_systemctl("is-active", "--quiet", unit.name)
                else:
                    run_systemctl("stop", unit.name)
        except (OSError, InstallationError) as recovery:
            raise InstallationError(f"服务启用失败（{original}），恢复也失败（{recovery}）。") from None
        detail = "已恢复先前的系统服务配置。" if previous is not None else "已保留初始部署文件。"
        raise InstallationError(f"服务启用失败（{original}），{detail}") from None


def check_bridge(release, config, credentials, additions, python=None):
    env = {key: value for key, value in os.environ.items() if key not in AMBIENT_LARK_CREDENTIALS}
    env.update(additions)
    try:
        result = subprocess.run(service_command("check", config, credentials, python), cwd=release,
                                env=env, stdin=subprocess.DEVNULL, timeout=90, check=False)
    except subprocess.TimeoutExpired:
        raise InstallationError("平台预检超时，旧服务配置保留。") from None
    if result.returncode:
        raise InstallationError("平台预检未通过，旧服务配置保留。")


def install(args):
    if sys.platform != "linux":
        raise InstallationError("部署入口需要 Linux。")
    if os.geteuid() == 0:
        raise InstallationError("请使用运行该团队服务的普通 Linux 用户，勿使用 sudo。")
    if NAME.fullmatch(args.team) is None:
        raise InstallationError("团队名请使用小写英文、数字和连字符。")
    os.umask(0o077)
    home = Path.home()
    config_home = Path(os.getenv("XDG_CONFIG_HOME") or home / ".config")
    data_home = Path(os.getenv("XDG_DATA_HOME") or home / ".local" / "share")
    if not config_home.is_absolute() or not data_home.is_absolute():
        raise InstallationError("XDG 配置和数据目录需要使用绝对路径。")
    config_root, data_root = (base / "omni-service" / args.team for base in (config_home, data_home))
    unit_name = f"omni-service-{args.team}.service"
    unit = config_home / "systemd" / "user" / unit_name
    if getattr(args, "fresh", False):
        for existing in (config_root, data_root, unit):
            if existing.exists() or existing.is_symlink():
                raise InstallationError(f"全新安装拒绝覆盖已有实例，请另选团队名。{existing}")
    private_directory(config_root)
    private_directory(data_root)
    config, credentials = config_root / "personal.json", config_root / "credentials.env"
    lark_home, database = config_root / "lark", data_root / "bridge.sqlite3"
    lark_data = config_root / "lark-data"
    private_directory(lark_home)
    private_directory(lark_data)
    unit_dir = config_home / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / unit_name
    with os.fdopen(os.open(config_root / ".install.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        incoming_config = args.config.read_bytes() if args.config else None
        if incoming_config is not None:
            inspect_config(incoming_config)
        incoming_secrets = private_bytes(args.secrets_file) if args.secrets_file else None
        if incoming_secrets is not None and (
            len(incoming_secrets) > 512_000 or b"\0" in incoming_secrets
        ):
            raise InstallationError("凭据文件无效。")
        saved = provision(config, incoming_config, (REPOSITORY / "config" / "personal.example.json").read_bytes())
        enabled = inspect_config(saved) in MODES
        provision(credentials, incoming_secrets, (
            "# OmniService private credentials\n"
            "TELEGRAM_BOT_TOKEN=\nOMNI_FEISHU_APP_SECRET=\nTRANSLATION_API_KEY=\n"
            f"OMNI_ADMIN_SIGNING_KEY={secrets.token_hex(32)}\n"
            "# TELEGRAM_HTTPS_PROXY=\n# TELEGRAM_TLS_TUNNEL_PORT=\n# LLM_API_KEY=\n"
        ).encode())
        managed = config_root / "tools" / "lark-cli"
        cli = str(managed) if managed.is_file() and os.access(managed, os.X_OK) else shutil.which("lark-cli")
        path_dirs = [str(Path(cli).absolute().parent)] if cli else []
        path_dirs.extend((str(home / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin"))
        path_dirs.extend(part for part in os.get_exec_path() if Path(part).is_absolute())
        release = package_sources(data_root)
        # The dependency bootstrap must be able to prepare a new SDK beside a
        # legacy PR #8 environment. This pass does not write or run a unit.
        python = (Path(sys.executable).absolute() if args.no_start and getattr(args, "defer_unit", False)
                  else private_python(config_root))
        additions = {
            "PYTHONPATH": str(release / "src"),
            "LARKSUITE_CLI_CONFIG_DIR": str(lark_home),
            "LARKSUITE_CLI_DATA_DIR": str(lark_data),
            "PATH": os.pathsep.join(dict.fromkeys(path_dirs)),
        }
        command = service_command("run", config, credentials, python) + ("--db", database)
        text = unit_text(args.team, release, command, additions)
        previous = private_bytes(unit) if unit.exists() or unit.is_symlink() else None
        start = enabled and not args.no_start
        print(f"配置\t{config}\n凭据\t{credentials}\n飞书配置\t{lark_home}\n飞书凭据目录\t{lark_data}\n"
              f"数据\t{database}\n版本\t{release}\n服务\t{unit_name}", flush=True)
        if not start:
            if previous is None and not getattr(args, "defer_unit", False):
                atomic_write(unit, text)
            print("已准备部署文件。本次未调用系统服务。")
            return
        if cli is None:
            raise InstallationError("配置已保存。请用 OmniService 接入入口加 --managed-dependencies 准备其私有飞书组件。")
        check_bridge(release, config, credentials, additions, python)
        activate_unit(unit, previous, text)
        print("系统服务已启动。通过服务日志检查平台连接。")


def inspect_config(data):
    if len(data) <= 512_000:
        try:
            mode = json.loads(data).get("mode")
            if mode == "disabled" or isinstance(mode, str) and mode in MODES:
                return mode
        except (UnicodeError, ValueError, AttributeError):
            pass
    raise InstallationError("个人接入配置无效，须使用仓库示例中的 mode。")


def main():
    parser = argparse.ArgumentParser(description="为当前 Linux 用户安装一套 OmniService。")
    parser.add_argument("team", help="团队名")
    parser.add_argument("--config", type=Path, help="首次安装时导入非密钥配置")
    parser.add_argument("--secrets-file", type=Path, help="首次安装时导入私有凭据文件")
    parser.add_argument("--no-start", action="store_true", help="只生成配置和系统服务文件")
    parser.add_argument("--fresh", action="store_true", help="仅创建全新实例，发现已有配置/数据/服务则退出")
    parser.add_argument("--defer-unit", action="store_true", help=argparse.SUPPRESS)
    try:
        install(parser.parse_args())
    except (OSError, InstallationError) as exc:
        print(f"安装未完成。{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
