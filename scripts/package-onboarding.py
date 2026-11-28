#!/usr/bin/env python3
"""Build a native Linux dependency bundle from reviewed official artifacts.

Run on the target architecture (Ubuntu 24.04 / CPython 3.12). No account or service
is created. --tools reuses a verified prepare directory without copying identity,
credentials, SDK environments or databases into the distributable.
"""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "deploy"), str(ROOT / "src")]

from dependency_bundle import expected_identity, verify_directory, digest_file, BundleError
from sdk_dependencies import download_wheels, verify_wheelhouse, SDKError
from setup_personal import ensure_tdlib, _download_source, _verify_source, SetupError
from omni_service.onboarding_tools import ensure_lark_cli, VERSION
from omni_service.models import ChannelError


def build(args):
    identity = expected_identity()
    if not args.source_only:
        import platform
        release_os = platform.freedesktop_os_release()
        if release_os.get("ID") != "ubuntu" or release_os.get("VERSION_ID") != "24.04":
            raise BundleError("原生发行包须在 Ubuntu 24.04 同架构构建；其他系统可用 --source-only。")
    output = args.output.absolute()
    checksum = Path(str(output) + ".sha256")
    if output.exists() or output.is_symlink() or checksum.exists() or checksum.is_symlink():
        raise BundleError("发行包或摘要文件已存在，未覆盖。")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".omni-package-", dir=output.parent) as temporary:
        work = Path(temporary)
        content = work / "content"
        content.mkdir()
        source = content / "tdlib-source.tar.gz"
        if args.tdlib_source:
            _verify_source(args.tdlib_source)
            shutil.copyfile(args.tdlib_source, source)
        else:
            _download_source(source)
        _verify_source(source)
        shutil.copyfile(ROOT / "requirements-onboarding.lock", content / "requirements-onboarding.lock")
        # Ubuntu's python3.12-venv need not expose a global pip. The disposable
        # builder is also used to resolve supplied wheels for this architecture.
        builder = work / "builder"
        subprocess.run((sys.executable, "-I", "-B", "-m", "venv", builder), check=True)
        if args.wheelhouse:
            verify_wheelhouse(args.wheelhouse)
            entry = ("import sys;sys.path.insert(0,sys.argv[1]);"
                     "from sdk_dependencies import check_wheelhouse_compatibility;"
                     "check_wheelhouse_compatibility(sys.argv[2])")
            subprocess.run((builder / "bin/python", "-I", "-B", "-c", entry,
                            ROOT / "deploy", args.wheelhouse.absolute()), check=True)
            shutil.copytree(args.wheelhouse, content / "wheelhouse")
        else:
            # Use a temporary builder venv: Ubuntu's python3.12-venv need not
            # expose a global pip, and the system interpreter stays untouched.
            entry = ("import sys;sys.path.insert(0,sys.argv[1]);"
                     "from sdk_dependencies import download_wheels;"
                     "download_wheels(sys.argv[2])")
            subprocess.run((builder / "bin/python", "-I", "-B", "-c", entry,
                            ROOT / "deploy", content / "wheelhouse"), check=True)
        if args.tools:
            tools = args.tools.absolute()
            if tools.name != "tools" or tools.is_symlink() or not tools.is_dir():
                raise BundleError("--tools 须为 prepare 生成的私有 tools 目录。")
        else:
            private = work / "instance"
            private.mkdir(mode=0o700)
            tools = private / "tools"
        ensure_lark_cli(tools.parent, archive=args.lark_archive)
        cli_name = f"lark-cli-{VERSION}-{identity['platform']}.tar.gz"
        shutil.copyfile(tools / cli_name, content / cli_name)
        if not args.source_only:
            binary = ensure_tdlib(tools, source, jobs=args.jobs)
            native = content / "native"
            native.mkdir()
            shutil.copyfile(binary, native / "libtdjson.so")
            # Loader and shared-library availability are checked by ensure_tdlib.
            # Record build OS for provenance; never label native amd64 as DGX.
            build_os = Path("/etc/os-release").read_text() if Path("/etc/os-release").is_file() else "unknown"
            (native / "build-environment.json").write_text(json.dumps({
                "platform": identity["platform"], "python": sys.version.split()[0],
                "os_release": build_os,
                "validation": "offline td_execute getOption(version); no account authorization",
            }, indent=2) + "\n")
        files = {path.relative_to(content).as_posix(): {"sha256": digest_file(path), "size": path.stat().st_size}
                 for path in sorted(content.rglob("*")) if path.is_file()}
        manifest = {**identity, "kind": "source" if args.source_only else "native", "files": files}
        (content / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        verify_directory(content)
        pending = work / "bundle.tar.gz"
        # Stable metadata; byte-identical inputs produce identical bundle hashes.
        with pending.open("xb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path in sorted(content.rglob("*")):
                    if path.is_file():
                        entry = tarfile.TarInfo(path.relative_to(content).as_posix())
                        entry.size, entry.mode, entry.mtime = path.stat().st_size, 0o600, 0
                        with path.open("rb") as stream:
                            archive.addfile(entry, stream)
        digest = digest_file(pending)
        # O_EXCL avoids clobbering an output created by a concurrent publisher.
        with output.open("xb") as destination, pending.open("rb") as incoming:
            shutil.copyfileobj(incoming, destination)
        with checksum.open("x") as sidecar:
            sidecar.write(f"{digest}  {output.name}\n")
    print(json.dumps({"bundle": str(output), "sha256": digest, "platform": identity["platform"],
                      "kind": manifest["kind"], "account_authorization": "not_attempted"}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-only", action="store_true", help="仅打包固定源码；安装端仍需编译工具")
    parser.add_argument("--tools", type=Path, help="复用 prepare 已验证的原生依赖，避免重编译")
    parser.add_argument("--tdlib-source", type=Path)
    parser.add_argument("--lark-archive", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        build(args)
    except (BundleError, SDKError, SetupError, ChannelError, OSError, ValueError,
            subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"依赖包未完成：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
