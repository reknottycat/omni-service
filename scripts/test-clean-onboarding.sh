#!/usr/bin/env bash
# Real dependency installation, with no account authorization or service start.
# online builds a native bundle; offline verifies it in a second, disconnected
# container. Both phases use fresh XDG directories beneath the supplied work dir.
set -euo pipefail
if [[ $# -ne 2 || ( $1 != online && $1 != offline ) ]]; then
    printf '%s\n' 'Usage: test-clean-onboarding.sh online|offline /absolute/work-directory' >&2
    exit 2
fi
omni_test_phase=$1
omni_test_work=$2
if [[ $omni_test_work != /* ]]; then
    printf '%s\n' 'The work directory must be absolute.' >&2
    exit 2
fi
omni_test_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
exec python3.12 -I -B - "$omni_test_repo" "$omni_test_phase" "$omni_test_work" <<'PY'
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys

repo, phase, work = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
def source_digest():
    digest = hashlib.sha256()
    inputs = [repo / "requirements-onboarding.lock"]
    for directory in ("deploy", "scripts", "src"):
        inputs.extend(p for p in (repo / directory).rglob("*")
                      if p.is_file() and p.suffix in (".py", ".sh", ".html", ".json"))
    for path in sorted(inputs):
        digest.update(path.relative_to(repo).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()

tested_source = source_digest()
if os.geteuid() == 0:
    raise SystemExit("Run the clean installation test as a non-root user.")
assert importlib.util.find_spec("lark_oapi") is None, "Global SDK would invalidate the clean test"
assert shutil.which("lark-cli") is None, "Global CLI would invalidate the clean test"
os.umask(0o077)
work.mkdir(mode=0o700, parents=True, exist_ok=True)
stage = work / phase
stage.mkdir(mode=0o700)  # Refuse to re-use an already tested instance.
config_root, data_root = stage / "config", stage / "data"
guard = stage / "guard-bin"
guard.mkdir()
(guard / "systemctl").write_text(
    '#!/bin/sh\nprintf "systemctl called\\n" >> "$OMNI_TEST_SYSTEMCTL_LOG"\nexit 91\n'
)
(guard / "systemctl").chmod(0o700)
# A broken installer must not silently consume an ambient lark-cli.
(guard / "lark-cli").write_text('#!/bin/sh\nexit 92\n')
(guard / "lark-cli").chmod(0o700)
env = {key: value for key, value in os.environ.items()
       if not key.startswith(("LARKSUITE_", "TELEGRAM_", "OMNI_", "PIP_", "PYTHON"))}
env.update({"XDG_CONFIG_HOME": str(config_root), "XDG_DATA_HOME": str(data_root),
            "PATH": str(guard) + ":/usr/local/bin:/usr/bin:/bin",
            "OMNI_TEST_SYSTEMCTL_LOG": str(stage / "systemctl-called"),
            "PYTHONDONTWRITEBYTECODE": "1"})
os.environ.update({key: env[key] for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")})
sys.path[:0] = [str(repo / "deploy"), str(repo / "src")]
import setup_personal as setup
from omni_service.onboarding_tools import VERSION as CLI_VERSION

def run(*args, success=True):
    result = subprocess.run(tuple(map(str, args)), env=env, cwd=repo, check=False)
    if success and result.returncode:
        raise AssertionError(f"Command failed ({result.returncode}): {args[0]}")
    if not success and result.returncode == 0:
        raise AssertionError("Invalid dependency unexpectedly accepted")
    return result

team = "clean-" + phase
bundle = work / "onboarding-linux.tar.gz"
options = []
if phase == "offline":
    assert not any(shutil.which(name) for name in ("cmake", "clang++", "g++")), \
        "Use the minimal runtime image: native bundles must not need a compiler"
    # The caller supplies an actual disconnected network namespace/container.
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=1):
            raise AssertionError("Offline test must run with networking disabled")
    except OSError:
        pass
    digest = (Path(str(bundle) + ".sha256")).read_text().split()[0]
    assert len(digest) == 64
    options = ["--bundle", bundle, "--bundle-sha256", digest]

run("bash", repo / "scripts/install-dgx.sh", team, *options)
instance = config_root / "omni-service" / team
tools = instance / "tools"
py_candidates = [p for p in tools.glob("python-*/bin/python") if p.is_file()]
assert len(py_candidates) == 1, "Exactly one private SDK interpreter is required"
py = py_candidates[0]
native_candidates = list(tools.rglob("libtdjson.so"))
assert len(native_candidates) == 1, "Exactly one verified native TDLib is required"
native = native_candidates[0]
run(sys.executable, "-I", repo / "deploy/setup_personal.py", "--probe-tdlib", native)
cli = subprocess.check_output((str(tools / "lark-cli"), "--version"), env=env, text=True).strip()
assert CLI_VERSION in cli
sdk = json.loads(subprocess.check_output((str(py), "-I", "-B", "-c",
    "import json; from importlib.metadata import version; "
    "from lark_oapi import aregister_app; assert callable(aregister_app); "
    "print(json.dumps({'lark_oapi': version('lark-oapi')}))"), env=env, text=True))
run(py, "-I", "-B", "-m", "pip", "--isolated", "check")

preserved = [instance / "personal.json", instance / "credentials.env",
             config_root / "systemd/user" / f"omni-service-{team}.service", native]
before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in preserved}
run("bash", repo / "scripts/setup-personal.sh", team, "prepare", *options)
after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in preserved}
assert before == after, "Retry changed saved configuration, credentials, unit or native library"
run("bash", repo / "scripts/install-dgx.sh", team, *options, success=False)
assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in preserved}
assert json.loads((instance / "personal.json").read_text())["mode"] == "disabled"
assert not list(data_root.rglob("*.sqlite3")), "prepare created account/runtime state"
assert not list((instance / "lark").rglob("*"))
assert not list((instance / "lark-data").rglob("*"))
assert not (stage / "systemctl-called").exists()
unit = preserved[2].read_text()
assert str(py) in unit, "Service would start outside the hash-locked private SDK environment"

if phase == "online":
    run(sys.executable, "-I", repo / "scripts/package-onboarding.py", "--output", bundle,
        "--tools", tools)
else:
    bad_bundle = stage / "corrupted.tar.gz"
    with bundle.open("rb") as source, bad_bundle.open("xb") as dest:
        shutil.copyfileobj(source, dest)
    with bad_bundle.open("r+b") as file:
        data = file.read(1)
        file.seek(0)
        file.write(bytes([data[0] ^ 1]))
    run("bash", repo / "scripts/setup-personal.sh", "tampered", "prepare",
        "--bundle", bad_bundle, "--bundle-sha256", digest, success=False)
    assert not (config_root / "omni-service/tampered/tools").exists()
    assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in preserved}

assert source_digest() == tested_source, "Source changed during the verification run"
result = {"phase": phase, "status": "passed", "source_sha256": tested_source,
          "os_release": Path("/etc/os-release").read_text(),
          "architecture": platform.machine(), "python": platform.python_version(),
          "libc": platform.libc_ver(), "global_sdk_and_cli_absent": True,
          "tdlib_version": setup.TD_VERSION, "tdlib_commit": setup.TD_REV,
          "tdlib_sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
          "lark_cli": cli, "sdk": sdk, "repeat_prepare_preserved_files": True,
          "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
          "existing_instance_refused": True,
          "offline_network_required": phase == "offline", "service_started": False,
          "compiler_absent": phase == "offline",
          "account_authorization": "not_run", "dgx_hardware": "not_tested"}
(work / f"clean-install-{phase}.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
PY
