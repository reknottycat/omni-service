"""Install the verified Feishu CLI into one team's private setup directory."""

import fcntl
import hashlib
import json
import os
import platform
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.request import urlopen

from .models import ChannelError

VERSION = "1.0.99"
_RELEASE = "https://github.com/larksuite/cli/releases/download/v" + VERSION
_SHA256 = {
    ("darwin", "amd64"): "31f900409082728761346a94b7b81038c78b302883b69c36b849b23d843d7de1",
    ("darwin", "arm64"): "f24f4106d1b0b1a4ee9a24ec0953dee6e518476bbe3d9758f78a732477525b21",
    ("linux", "amd64"): "fd29f51030dde6919b6ca9173bc87852d3846f253d198022ea2d9ee63527b945",
    ("linux", "arm64"): "2837145e72922d558538494d0da30ac6c509681fc0791f8818f4ddb87f5125b4",
}
_MAX_ARCHIVE = 32 * 1024 * 1024


def _file(path, *, private=False):
    """Open the exact regular file, rejecting links before hashing or execution."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    stream = os.fdopen(fd, "rb")
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or (private and (
            info.st_uid != os.geteuid() or info.st_mode & 0o077)):
        stream.close()
        raise ChannelError("lark_cli_package_unverified")
    return stream


def _hash(path, *, private=False):
    with _file(path, private=private) as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _copy_archive(source, target, digest, *, private=False):
    with _file(source, private=private) as incoming, target.open("xb") as output:
        if os.fstat(incoming.fileno()).st_size > _MAX_ARCHIVE:
            raise ChannelError("lark_cli_package_unverified")
        actual = hashlib.sha256()
        size = 0
        while block := incoming.read(128 * 1024):
            size += len(block)
            if size > _MAX_ARCHIVE:
                raise ChannelError("lark_cli_package_unverified")
            actual.update(block)
            output.write(block)
        if actual.hexdigest() != digest:
            raise ChannelError("lark_cli_package_unverified")
    target.chmod(0o600)


def _download_archive(url, target):
    with urlopen(url, timeout=20) as response, target.open("xb") as out:
        for _ in range(256):
            data = response.read(128 * 1024)
            if not data:
                target.chmod(0o600)
                return
            out.write(data)
    raise ChannelError("lark_cli_package_unverified")


def _extract_cli(archive, digest, target):
    with _file(archive) as source:
        if (os.fstat(source.fileno()).st_size > _MAX_ARCHIVE
                or hashlib.file_digest(source, "sha256").hexdigest() != digest):
            raise ChannelError("lark_cli_package_unverified")
        source.seek(0)
        with tarfile.open(fileobj=source, mode="r:gz") as bundle:
            entries = [entry for entry in bundle.getmembers() if entry.name == "lark-cli"]
            if len(entries) != 1:
                raise ChannelError("lark_cli_package_unverified")
            entry = entries[0]
            if not entry.isfile() or not 0 < entry.size <= 100 * 1024 * 1024:
                raise ChannelError("lark_cli_package_unverified")
            with bundle.extractfile(entry) as binary, target.open("xb") as output:
                for block in iter(lambda: binary.read(128 * 1024), b""):
                    output.write(block)
    target.chmod(0o700)


def _probe_cli(binary, system, arch):
    """Inspect architecture before executing only the digest-verified native file."""
    with _file(binary, private=True) as source:
        header = source.read(32)
    if system == "linux":
        valid = (header[:6] == b"\x7fELF\x02\x01" and len(header) >= 20
                 and int.from_bytes(header[18:20], "little") == {"amd64": 62, "arm64": 183}[arch])
    else:
        valid = (header[:4] == b"\xcf\xfa\xed\xfe" and len(header) >= 8
                 and int.from_bytes(header[4:8], "little") == {"amd64": 0x1000007, "arm64": 0x100000c}[arch])
    if not valid:
        raise ChannelError("lark_cli_package_unverified")
    # --version is offline. Do not pass the caller's account tokens/config into it.
    with tempfile.TemporaryDirectory(prefix=".lark-probe-", dir=binary.parent) as directory:
        env = {"LANG": "C", "LC_ALL": "C", "NO_COLOR": "1",
               "LARKSUITE_CLI_CONFIG_DIR": str(Path(directory) / "config"),
               "LARKSUITE_CLI_DATA_DIR": str(Path(directory) / "data")}
        result = subprocess.run((str(binary.absolute()), "--version"), env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                                check=False)
    if result.returncode or result.stdout.strip() != f"lark-cli version {VERSION}".encode():
        raise ChannelError("lark_cli_package_unverified")


def _install_cli(tools, system, arch, digest, supplied):
    name = f"lark-cli-{VERSION}-{system}-{arch}.tar.gz"
    cache = tools / name
    installed = tools / "lark-cli"
    receipt = tools / "lark-cli-install.json"
    with tempfile.TemporaryDirectory(prefix=".lark-install-", dir=tools) as temporary:
        staging = Path(temporary)
        archive = staging / name
        cached = cache.exists() or cache.is_symlink()
        # A corrupt managed cache is an error even when another archive is supplied.
        if cached and _hash(cache, private=True) != digest:
            raise ChannelError("lark_cli_package_unverified")
        if supplied is not None:
            _copy_archive(Path(supplied), archive, digest)
        elif cached:
            _copy_archive(cache, archive, digest, private=True)
        else:
            _download_archive(f"{_RELEASE}/{name}", archive)
        binary = staging / "lark-cli"
        _extract_cli(archive, digest, binary)
        identity = {"schema": 1, "version": VERSION, "platform": system, "arch": arch,
                    "archive_sha256": digest, "binary_sha256": _hash(binary, private=True)}
        existing = installed.exists() or installed.is_symlink()
        if existing:
            if (_hash(installed, private=True) != identity["binary_sha256"]
                    or not installed.stat().st_mode & stat.S_IXUSR):
                # Never repair by replacing another binary or a changed installation.
                raise ChannelError("lark_cli_package_unverified")
        recorded = receipt.exists() or receipt.is_symlink()
        if recorded:
            with _file(receipt, private=True) as saved:
                if json.loads(saved.read(4097)) != identity:
                    raise ChannelError("lark_cli_package_unverified")
        _probe_cli(binary, system, arch)
        if not cached:
            os.replace(archive, cache)
        if not existing:
            os.replace(binary, installed)
        # An earlier install without a receipt is adopted only after byte equality
        # against the pinned archive; its executable is left completely unchanged.
        if not recorded:
            pending = staging / "receipt.json"
            pending.write_text(json.dumps(identity, sort_keys=True) + "\n", encoding="utf-8")
            pending.chmod(0o600)
            os.replace(pending, receipt)


def ensure_lark_cli(directory, archive=None):
    """Install/revalidate the pinned CLI; an explicit archive never uses the network."""
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    system = platform.system().lower()
    digest = _SHA256.get((system, arch))
    if not digest:
        raise ChannelError("lark_cli_platform_unsupported")
    tools = Path(directory) / "tools"
    tools.mkdir(mode=0o700, exist_ok=True)
    info = tools.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ChannelError("lark_cli_private_tools_required")
    try:
        lock_path = tools / ".lark-install.lock"
        with os.fdopen(os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600), "wb") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ChannelError("lark_cli_private_tools_required")
            fcntl.flock(lock, fcntl.LOCK_EX)
            _install_cli(tools, system, arch, digest, archive)
    except (OSError, tarfile.TarError, KeyError, ValueError, subprocess.TimeoutExpired):
        raise ChannelError("lark_cli_install_failed") from None
    return tools
