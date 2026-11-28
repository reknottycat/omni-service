"""Hash-locked, private Feishu SDK environments, including offline installation.

No account, platform API, or system service is contacted by this module.  A
successful environment is immutable: validate its receipt before executing it.
"""

import base64
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements-onboarding.lock"
SDK_VERSION = "1.9.0"
RECEIPT = ".omni-sdk-receipt.json"
_NAME = re.compile(r"[-_.]+")


class SDKError(Exception):
    pass


def _hash(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _normal(name):
    return _NAME.sub("-", name).lower()


def _requirements():
    requirements = {}
    for entry in LOCK.read_text().replace("\\\n", " ").splitlines():
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s]+)((?:\s+--hash=sha256:[a-f0-9]{64})+)", entry)
        if not match or _normal(match[1]) in requirements:
            raise SDKError("飞书 SDK 锁文件须逐项固定版本和 SHA256。")
        requirements[_normal(match[1])] = {
            "version": match[2],
            "hashes": re.findall(r"sha256:([a-f0-9]{64})", match[3]),
        }
    if requirements.get("lark-oapi", {}).get("version") != SDK_VERSION:
        raise SDKError("飞书 SDK 版本与锁文件不一致。")
    return requirements


def _identity():
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
        raise SDKError("私有飞书 SDK 需要 CPython 3.12。")
    _requirements()
    return {
        "format": 1,
        "lock_sha256": _hash(LOCK),
        "python": platform.python_version(),
        "executable": str(Path(sys.executable).resolve()),
        "executable_sha256": _hash(Path(sys.executable)),
        "platform": sys.platform,
        "arch": platform.machine(),
    }


def _environment_path(tools, identity):
    # Include the interpreter identity: replacing the system interpreter creates
    # a new environment instead of changing one an existing deployment uses.
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    return tools / f"python-{sys.implementation.cache_tag}-{digest}"


def _private_directory(directory):
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SDKError("SDK 目录必须是当前用户私有的普通目录。")


def _snapshot(directory):
    files = {}
    base_python = Path(sys.executable).resolve()
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if relative == RECEIPT:
            continue
        info = path.lstat()
        if info.st_uid != os.getuid():
            raise SDKError("SDK 文件所有者不匹配。")
        if stat.S_ISLNK(info.st_mode):
            target = path.resolve()
            if not target.is_relative_to(directory.resolve()) and target != base_python:
                raise SDKError("SDK 文件包含指向外部的链接。")
            files[relative] = {"link": os.readlink(path)}
        elif stat.S_ISDIR(info.st_mode):
            files[relative] = {"directory": stat.S_IMODE(info.st_mode)}
        elif stat.S_ISREG(info.st_mode):
            files[relative] = {"sha256": _hash(path), "mode": stat.S_IMODE(info.st_mode)}
        else:
            raise SDKError("SDK 目录包含非普通文件。")
    return files


def _distributions(directory):
    site = directory / "lib" / "python3.12" / "site-packages"
    expected = {name: item["version"] for name, item in _requirements().items()}
    installed = {}
    for distribution in importlib.metadata.distributions(path=[str(site)]):
        name = _normal(distribution.metadata.get("Name", ""))
        if not name or name in installed:
            raise SDKError("SDK 包元数据无效或重复。")
        installed[name] = distribution.version
        if name != "pip" and expected.get(name) != distribution.version:
            raise SDKError("SDK 包版本或依赖集合与锁文件不匹配。")
        records = distribution.files
        if records is None:
            raise SDKError("SDK 包缺少 RECORD 文件。")
        for entry in records:
            path = Path(entry.locate()).resolve()
            if not path.is_relative_to(directory.resolve()) or not path.is_file():
                raise SDKError("SDK 包的 RECORD 包含无效路径。")
            if entry.hash is not None:
                if entry.hash.mode != "sha256":
                    raise SDKError("SDK 包的 RECORD 使用不支持的摘要。")
                digest = base64.urlsafe_b64encode(bytes.fromhex(_hash(path))).decode().rstrip("=")
                if digest != entry.hash.value:
                    raise SDKError("SDK 包文件与 RECORD 摘要不匹配。")
    if {name: version for name, version in installed.items() if name != "pip"} != expected:
        raise SDKError("SDK 包版本或依赖集合与锁文件不匹配。")
    return installed


def _validate(directory, identity):
    _private_directory(directory)
    receipt_path = directory / RECEIPT
    if not receipt_path.exists() or receipt_path.is_symlink():
        raise SDKError("SDK 目录不完整或缺少安装收据，已保留现有文件。")
    info = receipt_path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SDKError("SDK 收据必须是当前用户私有的普通文件。")
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, ValueError):
        raise SDKError("SDK 安装收据无法读取，已保留现有文件。") from None
    if not isinstance(receipt, dict) or receipt.get("identity") != identity or receipt.get("files") != _snapshot(directory):
        raise SDKError("SDK 缓存文件与安装收据不匹配，已拒绝执行。")
    if receipt.get("distributions") != _distributions(directory):
        raise SDKError("SDK 依赖与安装收据不匹配，已拒绝执行。")


def find_sdk(tools):
    """Return a verified existing interpreter, or None; never download/install."""
    tools = Path(tools).absolute()
    if not tools.exists() and not tools.is_symlink():
        return None
    _private_directory(tools)
    identity = _identity()
    directory = _environment_path(tools, identity)
    if not directory.exists() and not directory.is_symlink():
        return None
    _validate(directory, identity)
    return directory / "bin" / "python"


def _environment():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PIP_", "PYTHON", "LARKSUITE_CLI_"))
           and key not in ("VIRTUAL_ENV", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL")}
    # --isolated alone still loads global pip config; /dev/null disables it.
    env["PIP_CONFIG_FILE"] = os.devnull
    return env


def _run(command, *, timeout=300):
    try:
        result = subprocess.run(command, env=_environment(), stdin=subprocess.DEVNULL,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SDKError("SDK 依赖安装或离线检查失败，未发起账号授权。") from None
    if result.returncode:
        raise SDKError("SDK 依赖安装或离线检查失败，未发起账号授权。")


def verify_wheelhouse(wheelhouse):
    """Validate every wheel against the checked-in lock before pip reads it."""
    wheelhouse = Path(wheelhouse)
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise SDKError("离线 wheelhouse 必须是普通目录。")
    requirements = _requirements()
    allowed = {digest for value in requirements.values() for digest in value["hashes"]}
    files = list(wheelhouse.iterdir())
    if not files:
        raise SDKError("离线 wheelhouse 为空。")
    present = set()
    for wheel in files:
        if wheel.is_symlink() or not wheel.is_file() or wheel.suffix != ".whl":
            raise SDKError("离线 wheelhouse 包含未通过锁文件 SHA256 的文件。")
        digest = _hash(wheel)
        if digest not in allowed:
            raise SDKError("离线 wheelhouse 包含未通过锁文件 SHA256 的文件。")
        present.add(digest)
    if any(not present.intersection(requirement["hashes"]) for requirement in requirements.values()):
        raise SDKError("离线 wheelhouse 缺少锁文件中的依赖包。")
    # pip's resolver additionally checks target compatibility and metadata.
    return wheelhouse.absolute()


def _target_options(platform_tag):
    if platform_tag is None:
        return ()
    if platform_tag not in ("manylinux2014_aarch64", "manylinux2014_x86_64"):
        raise SDKError("不支持的 wheelhouse 目标平台。")
    return ("--platform", platform_tag, "--python-version", "3.12",
            "--implementation", "cp", "--abi", "cp312", "--abi", "abi3")


def check_wheelhouse_compatibility(wheelhouse, *, platform_tag=None):
    """Resolve a supplied wheelhouse without installing anything or networking."""
    wheelhouse = verify_wheelhouse(wheelhouse)
    extra = _target_options(platform_tag)
    with tempfile.TemporaryDirectory(prefix=".omni-wheel-check-") as target:
        # --target also makes dry-run work on PEP 668 managed system Pythons;
        # no permission override and no modification of that Python is needed.
        _run((sys.executable, "-I", "-B", "-m", "pip", "--isolated", "install",
              "--dry-run", "--ignore-installed", "--no-input", "--disable-pip-version-check",
              "--no-cache-dir", "--no-compile", "--require-hashes", "--only-binary=:all:",
              "--no-index", "--find-links", wheelhouse, *extra, "-r", LOCK, "--target", target))
    return wheelhouse


def download_wheels(destination, *, platform_tag=None):
    """Download a hash-verified wheelhouse, optionally for Linux ARM64/x86_64."""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise SDKError("wheelhouse 输出目录已存在，未覆盖。")
    _identity()
    extra = _target_options(platform_tag)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".wheelhouse-", dir=destination.parent) as staging:
        output = Path(staging) / "wheels"
        _run((sys.executable, "-I", "-B", "-m", "pip", "--isolated", "download",
              "--disable-pip-version-check", "--no-cache-dir", "--no-input",
              "--require-hashes", "--only-binary=:all:", "--index-url", "https://pypi.org/simple",
              *extra, "-r", LOCK, "--dest", output))
        verify_wheelhouse(output)
        output.rename(destination)
    return destination


def ensure_sdk(tools, wheelhouse=None):
    """Install or verify the complete SDK; a wheelhouse makes pip fully offline."""
    tools = Path(tools).absolute()
    _private_directory(tools)
    identity = _identity()
    directory = _environment_path(tools, identity)
    lock_path = tools / ".sdk-install.lock"
    with os.fdopen(os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600), "wb") as lock:
        lock_info = os.fstat(lock.fileno())
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid() or lock_info.st_mode & 0o077:
            raise SDKError("SDK 安装锁必须是当前用户私有的普通文件。")
        fcntl.flock(lock, fcntl.LOCK_EX)
        source = ("--index-url", "https://pypi.org/simple")
        if wheelhouse is not None:
            source = ("--no-index", "--find-links", verify_wheelhouse(wheelhouse))
        cached = find_sdk(tools)
        if cached is not None:
            return cached
        directory.mkdir(mode=0o700)
        try:
            # Build at its final private path. Moving a venv leaves stale pip
            # shebangs; no interpreter path is returned until receipt commit.
            _run((sys.executable, "-I", "-B", "-m", "venv", directory), timeout=90)
            python = directory / "bin" / "python"
            _run((python, "-I", "-B", "-m", "pip", "--isolated", "install", "--no-input",
                  "--disable-pip-version-check", "--no-cache-dir", "--no-compile", "--require-hashes",
                  "--only-binary=:all:", *source, "-r", LOCK))
            _run((python, "-I", "-B", "-m", "pip", "--isolated", "check"), timeout=30)
            distributions = _distributions(directory)
            probe = ("from importlib.metadata import version; "
                     "from lark_oapi import aregister_app; "
                     f"assert version('lark-oapi') == '{SDK_VERSION}' and callable(aregister_app)")
            _run((python, "-I", "-B", "-c", probe), timeout=45)
            receipt = {"identity": identity, "distributions": distributions, "files": _snapshot(directory)}
            fd = os.open(directory / RECEIPT, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(receipt, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            _validate(directory, identity)
            return python
        except BaseException:
            # Only this newly created, unpublished environment is removed.
            shutil.rmtree(directory)
            raise
