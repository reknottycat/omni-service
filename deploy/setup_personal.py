"""Prepare and use OmniService's private onboarding dependencies, without touching a running service."""

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
TD_REV = "1b9efe84d6e9a2e2f6eadeeea32b0726d2b8c25d"
TD_SHA = "e7d319970e62bfb48dbccc0a6b0d9a14361a0df04866332f5b6e1db1ca1a28c2"
TD_VERSION = "1.8.71"
EXPORTS = ("td_create_client_id", "td_send", "td_receive", "td_execute")


class SetupError(Exception):
    pass


def _native_version(path):
    lib = ctypes.CDLL(str(path))
    for name in EXPORTS:
        getattr(lib, name)
    lib.td_execute.argtypes = (ctypes.c_char_p,)
    lib.td_execute.restype = ctypes.c_char_p
    result = json.loads(lib.td_execute(b'{"@type":"getOption","name":"version"}') or b"null")
    if result != {"@type": "optionValueString", "value": TD_VERSION}:
        raise SetupError("TDLib 版本回读不匹配。")


def _hash(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _verify_source(archive):
    if not archive.is_file() or archive.stat().st_size > 12 * 1024 * 1024 or _hash(archive) != TD_SHA:
        raise SetupError("官方 TDLib 源码校验不匹配，未解包或构建。")


def _download_source(archive):
    url = "https://codeload.github.com/tdlib/td/tar.gz/" + TD_REV
    with urlopen(url, timeout=20) as response, archive.open("xb") as output:
        data = response.read(12 * 1024 * 1024 + 1)
        if len(data) > 12 * 1024 * 1024:
            raise SetupError("TDLib 源码包超出大小上限。")
        output.write(data)


def _run(argv, *, env=None, limit=None):
    if subprocess.run(argv, env=env, timeout=limit, check=False).returncode:
        raise SetupError("依赖安装或检查失败，未发起账号授权。")


def _offline_probe(target):
    # Separate process: never creates a Telegram client or reads account credentials.
    _run((sys.executable, "-I", __file__, "--probe-tdlib", target), limit=20)


def ensure_tdlib(tools, source_archive=None):
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    binary = tools / ("libtdjson" + suffix)
    receipt = tools / "tdlib-install.json"
    identity = {"commit": TD_REV, "platform": sys.platform, "arch": platform.machine()}
    if binary.exists() or binary.is_symlink() or receipt.exists() or receipt.is_symlink():
        for item in (binary, receipt):
            if not stat.S_ISREG(item.lstat().st_mode) or item.stat().st_uid != os.getuid() or item.stat().st_mode & 0o077:
                raise SetupError("已存在非本安装器管理的 TDLib，保留原文件。")
        if json.loads(receipt.read_text()) != {**identity, "sha256": _hash(binary)}:
            raise SetupError("现有 TDLib 与安装收据不匹配，保留原文件。")
        _offline_probe(binary)
        return binary
    with tempfile.TemporaryDirectory(prefix=".tdlib-", dir=tools) as cwd:
        work = Path(cwd)
        archive = work / "source.tar.gz"
        if source_archive is None:
            _download_source(archive)
        else:
            _verify_source(source_archive)
            with source_archive.open("rb") as given, archive.open("xb") as dest:
                dest.write(given.read(12 * 1024 * 1024 + 1))
        _verify_source(archive)
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(work, filter="data")
        source = work / ("td-" + TD_REV)
        build = work / "build"
        options = ["-DCMAKE_BUILD_TYPE=Release", "-DOPENSSL_USE_STATIC_LIBS=ON"]
        if sys.platform == "darwin" and Path("/opt/homebrew/opt/openssl@3").is_dir():
            options.append("-DOPENSSL_ROOT_DIR=/opt/homebrew/opt/openssl@3")
        try:
            _run(("cmake", "-S", source, "-B", build, *options))
            _run(("cmake", "--build", build, "--target", "tdjson", "--parallel", "2"))
        except FileNotFoundError:
            raise SetupError("构建 Telegram 组件需要 CMake、C++、OpenSSL 和 zlib 开发工具；不需要安装 Telegram CLI。") from None
        name = f"libtdjson.{TD_VERSION}.dylib" if sys.platform == "darwin" else f"libtdjson.so.{TD_VERSION}"
        compiled = build / name
        compiled.chmod(0o700)
        _offline_probe(compiled)
        checkpoint = work / "receipt"
        checkpoint.write_text(json.dumps({**identity, "sha256": _hash(compiled)}) + "\n")
        compiled.replace(binary)
        checkpoint.replace(receipt)
    return binary


def ensure_sdk(tools):
    lock = ROOT / "requirements-onboarding.lock"
    name = f"python-{sys.implementation.cache_tag}-{_hash(lock)[:24]}"
    venv = tools / name
    test_import = "from importlib.metadata import version; from lark_oapi import aregister_app; assert version('lark-oapi') == '1.9.0' and callable(aregister_app)"
    if venv.is_symlink():
        raise SetupError("私有飞书 SDK 路径不能是符号链接。")
    if not venv.exists():
        with tempfile.TemporaryDirectory(prefix=".sdk-", dir=tools) as work:
            staged = Path(work) / "sdk"
            _run((sys.executable, "-I", "-m", "venv", staged), limit=90)
            py = staged / "bin" / "python"
            # Do not use user mirrors, user pip settings, source builds or global packages.
            env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
            env["PIP_CONFIG_FILE"] = os.devnull
            _run((py, "-I", "-m", "pip", "--isolated", "install", "--no-input",
                  "--disable-pip-version-check", "--no-cache-dir", "--require-hashes",
                  "--only-binary=:all:", "--index-url", "https://pypi.org/simple", "-r", lock), env=env, limit=240)
            _run((py, "-I", "-m", "pip", "--isolated", "check"), limit=30)
            _run((py, "-I", "-c", test_import), limit=45)
            staged.rename(venv)
    py = venv / "bin" / "python"
    _run((py, "-I", "-c", test_import), limit=45)
    return py


def verify_app_directory(root):
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or root.resolve(strict=True) != root):
        raise SetupError("团队目录必须为当前用户专用且不可为符号链接。")


def check_telegram_app(target, validate):
    if not target.exists() and not target.is_symlink():
        return "missing"
    validate(target)
    return "ready"


def store_telegram_app(target, validate):
    from omni_service.telegram_tdlib import _validate_application, private_input

    if check_telegram_app(target, validate) == "ready":
        return
    app_id = private_input("Telegram App api_id（隐藏输入）: ")
    if not isinstance(app_id, str) or not app_id.isascii() or not app_id.isdecimal() or len(app_id) > 10:
        raise SetupError("Telegram App api_id 无效。")
    app_hash = private_input("Telegram App api_hash（隐藏输入）: ")
    api_id, api_hash = _validate_application(int(app_id), app_hash)
    persist_telegram_app(target, api_id, api_hash)


def persist_telegram_app(target, api_id, api_hash):
    with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            json.dump({"api_id": api_id, "api_hash": api_hash}, stream)
            stream.flush()
            os.fsync(stream.fileno())
            # Publish the complete validated file only if no file already exists.
            os.link(staged, target, follow_symlinks=False)
        finally:
            staged.unlink(missing_ok=True)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "--probe-tdlib":
        try:
            _native_version(Path(argv[1]))
        except (SetupError, OSError, ValueError, AttributeError):
            print("TDLib 载入或离线 API 版本验证失败。", file=sys.stderr)
            return 1
        return 0
    setup, separator, options = list(argv), "--", []
    if separator in setup:
        i = setup.index(separator)
        setup, options = setup[:i], setup[i + 1:]
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("team")
    parser.add_argument("action", choices=("prepare", "register-feishu", "configure", "check-feishu-scopes",
                                           "telegram-app-store", "telegram-app-status", "telegram-app-enroll"))
    parser.add_argument("--create-telegram-app", action="store_true",
                        help="明确允许在官方开发者后台登记一个 OmniService 应用（已存在则不改）")
    parser.add_argument("--tdlib-source", type=Path, help="可选：用固定校验的官方源码包离线构建 TDLib")
    args = parser.parse_args(setup)
    if any(x.split("=", 1)[0] in ("--config", "--secrets-file", "--tdlib-library") for x in options):
        parser.error("接入入口使用本实例固定配置、凭据文件和已验证的 TDLib")
    if args.create_telegram_app and args.action != "telegram-app-enroll":
        parser.error("--create-telegram-app 仅用于 telegram-app-enroll")
    if args.action in ("prepare", "telegram-app-store", "telegram-app-status", "telegram-app-enroll") and options:
        parser.error("此操作不接受接入参数")
    os.umask(0o077)
    sys.path.insert(0, str(ROOT / "src"))
    from omni_service.models import ChannelError
    from omni_service.onboarding import _setup_lock
    from omni_service.onboarding_tools import ensure_lark_cli
    from omni_service.telegram_tdlib import application_credentials
    try:
        xdg = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
        if not xdg.is_absolute():
            raise SetupError("XDG 配置目录必须是绝对路径。")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", args.team):
            raise SetupError("团队名请使用小写英文、数字和连字符。")
        root = xdg / "omni-service" / args.team
        if args.action in ("telegram-app-store", "telegram-app-status", "telegram-app-enroll"):
            verify_app_directory(root)
            target = root / "telegram-application.json"
            if args.action == "telegram-app-status":
                status = check_telegram_app(target, application_credentials)
                print(json.dumps({"telegram_application": status,
                                  "scope": "local_format_only", "login_started": False}))
                return 0 if status == "ready" else 1
            with _setup_lock(root / ".dependencies.lock"):
                if args.action == "telegram-app-enroll":
                    if check_telegram_app(target, application_credentials) == "missing":
                        from omni_service.telegram_developer import enroll
                        from omni_service.telegram_tdlib import private_input
                        phone = private_input("Telegram 开发者账号手机号（隐藏输入）: ").strip()
                        api_id, api_hash = enroll(
                            phone, lambda: private_input("开发者后台本次验证码（隐藏输入）: ").strip(),
                            create=args.create_telegram_app)
                        persist_telegram_app(target, api_id, api_hash)
                else:
                    store_telegram_app(target, application_credentials)
            print("Telegram 开发者应用凭据可用。未发起登录或绑定。")
            return 0
        installer = ROOT / "deploy" / "install_personal.py"
        _run((sys.executable, "-I", installer, args.team, "--no-start"), limit=60)
        with _setup_lock(root / ".dependencies.lock"):
            tools = ensure_lark_cli(root)
            py = ensure_sdk(tools)
            if args.action in ("prepare", "configure"):
                ensure_tdlib(tools, args.tdlib_source)
        if args.action == "prepare":
            print("飞书和 Telegram 组件准备并通过本机检查；未启动服务或登录平台。")
            return 0
        target = root / "telegram-application.json"
        keys = {item.split("=", 1)[0] for item in options}
        if (args.action == "configure" and "--telegram-owner" in keys
                and "--tdlib-app-credentials" not in keys and (target.exists() or target.is_symlink())):
            application_credentials(target)
            options.extend(("--tdlib-app-credentials", target))
        entry = ("import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
                 "runpy.run_module('omni_service.onboarding', run_name='__main__')")
        return subprocess.run((py, "-I", "-B", "-c", entry, ROOT / "src", args.action,
                               *options, "--managed-dependencies", "--config", root / "personal.json",
                               "--secrets-file", root / "credentials.env"), check=False).returncode
    except (SetupError, OSError, ChannelError, ValueError, subprocess.TimeoutExpired) as exc:
        detail = str(exc) if isinstance(exc, (SetupError, ChannelError)) else type(exc).__name__
        print(f"OmniService 接入准备未完成：{detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
