"""Install the verified Feishu CLI into one team's private setup directory."""

import hashlib
import os
import platform
import stat
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


def _download_archive(url, target):
    with urlopen(url, timeout=20) as response, target.open("xb") as out:
        for _ in range(256):
            data = response.read(128 * 1024)
            if not data:
                return
            out.write(data)
    raise ChannelError("lark_cli_package_unverified")


def _extract_cli(archive, digest, target):
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != digest:
            raise ChannelError("lark_cli_package_unverified")
        source.seek(0)
        with tarfile.open(fileobj=source, mode="r:gz") as bundle:
            entry = bundle.getmember("lark-cli")
            if not entry.isfile() or not 0 < entry.size <= 100 * 1024 * 1024:
                raise ChannelError("lark_cli_package_unverified")
            with bundle.extractfile(entry) as binary, target.open("xb") as output:
                for block in iter(lambda: binary.read(128 * 1024), b""):
                    output.write(block)
    target.chmod(0o700)


def ensure_lark_cli(directory):
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    system = platform.system().lower()
    digest = _SHA256.get((system, arch))
    if not digest:
        raise ChannelError("lark_cli_platform_unsupported")
    tools = directory / "tools"
    tools.mkdir(mode=0o700, exist_ok=True)
    info = tools.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ChannelError("lark_cli_private_tools_required")
    name = f"lark-cli-{VERSION}-{system}-{arch}.tar.gz"
    # Keep the verified archive so every install/retry revalidates actual bytes.
    cache = tools / name
    try:
        with tempfile.TemporaryDirectory(prefix=".lark-install-", dir=tools) as temporary:
            staging = Path(temporary)
            archive = staging / name
            from_cache = cache.exists() or cache.is_symlink()
            if from_cache:
                if not stat.S_ISREG(cache.lstat().st_mode):
                    raise ChannelError("lark_cli_package_unverified")
                archive = cache
            else:
                _download_archive(f"{_RELEASE}/{name}", archive)
            binary = staging / "lark-cli"
            _extract_cli(archive, digest, binary)
            if not from_cache:
                os.replace(archive, cache)
            os.replace(binary, tools / "lark-cli")
    except (OSError, tarfile.TarError, KeyError) as exc:
        raise ChannelError("lark_cli_install_failed") from None
    return tools
