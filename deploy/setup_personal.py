"""Prepare and use OmniService's private onboarding dependencies, without touching a running service."""

import argparse
from contextlib import nullcontext
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from dependency_pins import TD_REV, TD_SHA, TD_VERSION, TD_SOURCE_URL, EXPORTS


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
    with urlopen(TD_SOURCE_URL, timeout=60) as response, archive.open("xb") as output:
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


def _verify_native_cache(binary, receipt, identity):
    for item in (binary, receipt):
        if (not item.exists() or not stat.S_ISREG(item.lstat().st_mode)
                or item.stat().st_uid != os.getuid() or item.stat().st_mode & 0o077):
            raise SetupError("TDLib 缓存不完整或并非本用户私有文件，保留原文件。")
    if json.loads(receipt.read_text()) != {**identity, "sha256": _hash(binary)}:
        raise SetupError("现有 TDLib 与安装收据不匹配，保留原文件。")
    _offline_probe(binary)
    return binary


def ensure_tdlib(tools, source_archive=None, *, native_library=None, native_sha256=None, jobs=2):
    """Install a verified native bundle, or build fixed official source as fallback.

    A directory rename publishes both binary and receipt together. Legacy PR #8
    files remain readable and are never silently replaced.
    """
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    identity = {"commit": TD_REV, "platform": sys.platform, "arch": platform.machine()}
    binary, receipt = tools / ("libtdjson" + suffix), tools / "tdlib-install.json"
    if binary.exists() or binary.is_symlink() or receipt.exists() or receipt.is_symlink():
        return _verify_native_cache(binary, receipt, identity)
    destination = tools / f"tdlib-{TD_REV}-{sys.platform}-{platform.machine()}"
    if destination.exists() or destination.is_symlink():
        info = destination.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise SetupError("TDLib 安装目录必须为当前用户私有的普通目录。")
        return _verify_native_cache(destination / binary.name, destination / receipt.name, identity)
    if not 1 <= jobs <= 16:
        raise SetupError("编译并发数必须在 1 到 16 之间。")
    with tempfile.TemporaryDirectory(prefix=".tdlib-", dir=tools) as cwd:
        work = Path(cwd)
        staged = work / "installed"
        staged.mkdir(mode=0o700)
        target = staged / binary.name
        if native_library is not None:
            if (not native_library.is_file() or native_library.is_symlink()
                    or not native_sha256 or _hash(native_library) != native_sha256):
                raise SetupError("打包的 TDLib 二进制摘要不匹配，未载入。")
            shutil.copyfile(native_library, target)
            if _hash(target) != native_sha256:
                raise SetupError("复制后的 TDLib 二进制摘要不匹配，未载入。")
        else:
            archive = work / "source.tar.gz"
            if source_archive is None:
                _download_source(archive)
            else:
                _verify_source(source_archive)
                with source_archive.open("rb") as given, archive.open("xb") as dest:
                    dest.write(given.read(12 * 1024 * 1024 + 1))
            _verify_source(archive)
            missing = [name for name in ("cmake", "gperf") if shutil.which(name) is None]
            if missing:
                raise SetupError("缺少编译工具：" + ", ".join(missing)
                                 + "；新机器可运行 scripts/install-dgx.sh TEAM --install-system-deps。")
            with tarfile.open(archive, "r:gz") as bundle:
                bundle.extractall(work, filter="data")
            source = work / ("td-" + TD_REV)
            build = work / "build"
            options = ["-DCMAKE_BUILD_TYPE=Release", "-DOPENSSL_USE_STATIC_LIBS=ON"]
            if sys.platform == "darwin" and Path("/opt/homebrew/opt/openssl@3").is_dir():
                options.append("-DOPENSSL_ROOT_DIR=/opt/homebrew/opt/openssl@3")
            _run(("cmake", "-S", source, "-B", build, *options), limit=300)
            _run(("cmake", "--build", build, "--target", "tdjson", "--parallel", str(jobs)), limit=5400)
            name = f"libtdjson.{TD_VERSION}.dylib" if sys.platform == "darwin" else f"libtdjson.so.{TD_VERSION}"
            shutil.copyfile(build / name, target)
        target.chmod(0o700)
        _offline_probe(target)
        checkpoint = staged / receipt.name
        checkpoint.touch(mode=0o600)
        checkpoint.write_text(json.dumps({**identity, "sha256": _hash(target)}) + "\n")
        staged.rename(destination)
    return destination / binary.name


def ensure_sdk(tools, wheelhouse=None):
    from sdk_dependencies import ensure_sdk as install_sdk, SDKError
    try:
        return install_sdk(tools, wheelhouse)
    except SDKError as exc:
        raise SetupError(str(exc)) from None


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
    parser.add_argument("action", choices=("prepare", "register-feishu", "configure", "check-feishu-scopes"))
    parser.add_argument("--tdlib-source", type=Path, help="可选：用固定校验的官方源码包离线构建 TDLib")
    parser.add_argument("--bundle", type=Path, help="此架构的完整依赖发行包；使用时不下载 Python/CLI/TDLib")
    parser.add_argument("--bundle-sha256", help="从可信发布渠道取得的发行包 SHA256")
    parser.add_argument("--jobs", type=int, default=2, help="源码构建并发数，默认 2")
    parser.add_argument("--fresh", action="store_true", help="拒绝已有实例的配置、数据或服务文件")
    args = parser.parse_args(setup)
    if any(x.split("=", 1)[0] in ("--config", "--secrets-file", "--tdlib-library") for x in options):
        parser.error("接入入口使用本实例固定配置、凭据文件和已验证的 TDLib")
    if args.action == "prepare" and options:
        parser.error("prepare 不接受接入参数")
    if bool(args.bundle) != bool(args.bundle_sha256) or (args.bundle and args.tdlib_source):
        parser.error("--bundle 必须同时提供 --bundle-sha256，且不能与 --tdlib-source 混用")
    if not 1 <= args.jobs <= 16:
        parser.error("--jobs 必须为 1 到 16")
    os.umask(0o077)
    sys.path.insert(0, str(ROOT / "src"))
    from omni_service.models import ChannelError
    from omni_service.onboarding import _setup_lock
    from omni_service.onboarding_tools import ensure_lark_cli
    from dependency_bundle import unpack_bundle, BundleError
    try:
        if sys.version_info[:2] != (3, 12) or sys.implementation.name != "cpython":
            raise SetupError("当前固定依赖集需要 CPython 3.12。")
        xdg = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
        if not xdg.is_absolute():
            raise SetupError("XDG 配置目录必须是绝对路径。")
        installer = ROOT / "deploy" / "install_personal.py"
        root = xdg / "omni-service" / args.team
        # Validate an offline bundle before creating any deployment files.
        bundle_context = unpack_bundle(args.bundle, args.bundle_sha256) if args.bundle else nullcontext((None, None))
        with bundle_context as (bundle, manifest):
            first_install = (sys.executable, "-I", installer, args.team, "--no-start", "--defer-unit")
            if args.fresh:
                first_install += ("--fresh",)
            _run(first_install, limit=60)
            with _setup_lock(root / ".dependencies.lock"):
                if bundle:
                    from omni_service.onboarding_tools import VERSION
                    cli_archive = bundle / f"lark-cli-{VERSION}-{manifest['platform']}.tar.gz"
                    tools = ensure_lark_cli(root, archive=cli_archive)
                    py = ensure_sdk(tools, bundle / "wheelhouse")
                else:
                    tools = ensure_lark_cli(root)
                    py = ensure_sdk(tools)
                native = None
                if args.action in ("prepare", "configure"):
                    if bundle:
                        component = bundle / "native" / "libtdjson.so"
                        native_options = {"jobs": args.jobs}
                        if manifest["kind"] == "native":
                            native_options.update(native_library=component,
                                                  native_sha256=manifest["files"]["native/libtdjson.so"]["sha256"])
                        native = ensure_tdlib(tools, bundle / "tdlib-source.tar.gz", **native_options)
                    elif args.jobs == 2:
                        native = ensure_tdlib(tools, args.tdlib_source)
                    else:
                        native = ensure_tdlib(tools, args.tdlib_source, jobs=args.jobs)
            # Create the first unit only after the private SDK is available.
            # Existing units (including user-customized drafts) are never changed.
            _run((py, "-I", "-B", installer, args.team, "--no-start"), limit=90)
        if args.action == "prepare":
            print("飞书和 Telegram 组件准备并通过本机检查；未启动服务或登录平台。")
            return 0
        entry = ("import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
                 "runpy.run_module('omni_service.onboarding', run_name='__main__')")
        if native is not None and args.action == "configure" and any(
                value.split("=", 1)[0] == "--telegram-owner" for value in options):
            options += ["--tdlib-library", str(native)]
        return subprocess.run((py, "-I", "-B", "-c", entry, ROOT / "src", args.action,
                               *options, "--managed-dependencies", "--config", root / "personal.json",
                               "--secrets-file", root / "credentials.env"), check=False).returncode
    except (SetupError, BundleError, OSError, ChannelError, ValueError, subprocess.TimeoutExpired) as exc:
        detail = str(exc) if isinstance(exc, (SetupError, BundleError, ChannelError)) else type(exc).__name__
        print(f"OmniService 接入准备未完成：{detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
