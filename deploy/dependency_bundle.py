"""A digest-pinned, platform-specific transport for the existing onboarding dependencies.

The caller supplies the bundle digest from its trusted release channel. The manifest
does not authenticate itself. Upstream source, CLI and wheel hashes are checked again
by their installers, independently of the bundle manifest.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import sys
import tarfile
import tempfile

from dependency_pins import TD_REV, TD_SHA, TD_VERSION

ROOT = Path(__file__).resolve().parents[1]
MAX_BUNDLE = 512 * 1024 * 1024
MAX_EXPANDED = 768 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")


class BundleError(Exception):
    pass


def digest_file(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def platform_key():
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if sys.platform != "linux" or arch is None or sys.version_info[:2] != (3, 12):
        raise BundleError("依赖发行包需要 Linux amd64/arm64 和 CPython 3.12。")
    return "linux-" + arch


def expected_identity():
    from omni_service.onboarding_tools import VERSION, _SHA256
    system, arch = platform_key().split("-")
    return {
        "schema": 1, "platform": platform_key(), "python": "cpython-3.12",
        "tdlib": {"commit": TD_REV, "version": TD_VERSION, "source_sha256": TD_SHA},
        "lark_cli": {"version": VERSION, "archive_sha256": _SHA256[(system, arch)]},
        "sdk_lock_sha256": digest_file(ROOT / "requirements-onboarding.lock"),
    }


def _valid_name(name):
    path = PurePosixPath(name)
    return (bool(name) and not path.is_absolute() and "\\" not in name
            and all(part not in ("", ".", "..") for part in name.split("/")))


def verify_directory(root):
    try:
        manifest = json.loads((root / "manifest.json").read_bytes())
        if not isinstance(manifest, dict):
            raise BundleError("依赖包清单必须是 JSON 对象。")
        identity = expected_identity()
        if any(manifest.get(key) != value for key, value in identity.items()):
            raise BundleError("依赖包平台、Python 或固定版本与当前代码不匹配。")
        files = manifest["files"]
        if not isinstance(files, dict) or not files or len(files) > 200:
            raise BundleError("依赖包文件清单无效。")
        actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        if actual != set(files) | {"manifest.json"}:
            raise BundleError("依赖包文件清单不完整。")
        for name, details in files.items():
            if not _valid_name(name) or name == "manifest.json":
                raise BundleError("依赖包包含无效路径。")
            target = root / name
            if (not isinstance(details, dict) or target.is_symlink()
                    or not SHA256.fullmatch(str(details.get("sha256", "")))
                    or target.stat().st_size != details.get("size")
                    or digest_file(target) != details["sha256"]):
                raise BundleError("依赖包内部文件摘要不匹配。")
        from omni_service.onboarding_tools import VERSION
        required = {"tdlib-source.tar.gz", "requirements-onboarding.lock",
                    f"lark-cli-{VERSION}-{identity['platform']}.tar.gz"}
        if not required.issubset(files) or not any(name.startswith("wheelhouse/") for name in files):
            raise BundleError("依赖包缺少固定源码、CLI 或 SDK wheels。")
        if digest_file(root / "tdlib-source.tar.gz") != TD_SHA:
            raise BundleError("依赖包 TDLib 源码摘要与固定版本不匹配。")
        if digest_file(root / "requirements-onboarding.lock") != identity["sdk_lock_sha256"]:
            raise BundleError("依赖包 SDK 锁文件与当前代码不匹配。")
        cli_name = f"lark-cli-{VERSION}-{identity['platform']}.tar.gz"
        if digest_file(root / cli_name) != identity["lark_cli"]["archive_sha256"]:
            raise BundleError("依赖包 CLI 摘要与固定官方版本不匹配。")
        from sdk_dependencies import verify_wheelhouse, SDKError
        try:
            verify_wheelhouse(root / "wheelhouse")
        except SDKError as exc:
            raise BundleError(str(exc)) from None
        native = "native/libtdjson.so" in files
        if manifest.get("kind") != ("native" if native else "source"):
            raise BundleError("依赖包类型与原生组件不匹配。")
        return manifest
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise BundleError("依赖包清单或内容无效。") from exc


@contextmanager
def unpack_bundle(archive, expected_sha256):
    """Verify compressed bytes before opening tar; never extract links or devices."""
    archive = Path(archive)
    if not SHA256.fullmatch(expected_sha256 or ""):
        raise BundleError("依赖包 SHA256 不匹配或未提供受信摘要，未解包或执行。")
    with tempfile.TemporaryDirectory(prefix="omni-bundle-") as temporary:
        private_archive = Path(temporary) / "verified.tar.gz"
        root = Path(temporary) / "content"
        root.mkdir()
        total, seen = 0, set()
        try:
            fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source, private_archive.open("xb") as destination:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BUNDLE:
                    raise BundleError("依赖包不是大小范围内的普通文件。")
                digest, size = hashlib.sha256(), 0
                while block := source.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_BUNDLE:
                        raise BundleError("依赖包超出大小上限。")
                    digest.update(block)
                    destination.write(block)
                if digest.hexdigest() != expected_sha256:
                    raise BundleError("依赖包 SHA256 不匹配，未解包或执行。")
            # Only the private byte-for-byte snapshot is reopened. Swapping the
            # caller's original file after verification cannot change a native lib.
            with tarfile.open(private_archive, "r:gz") as bundle:
                for entry in bundle:
                    if (not entry.isfile() or not _valid_name(entry.name)
                            or entry.name in seen or len(seen) >= 201):
                        raise BundleError("依赖包只允许清单内的唯一普通文件。")
                    total += entry.size
                    if entry.size < 0 or total > MAX_EXPANDED:
                        raise BundleError("依赖包超出大小上限。")
                    seen.add(entry.name)
                    target = root / entry.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.extractfile(entry) as source, target.open("xb") as dest:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            dest.write(block)
            manifest = verify_directory(root)
            yield root, manifest
        except (OSError, tarfile.TarError, EOFError) as exc:
            raise BundleError("依赖包读取失败；未使用未校验的组件。") from exc
