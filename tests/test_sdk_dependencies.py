"""Offline installer tests use a locally generated wheel, never platform accounts."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


PATH = Path(__file__).resolve().parents[1] / "deploy" / "sdk_dependencies.py"
SPEC = importlib.util.spec_from_file_location("sdk_dependencies", PATH)
sdk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sdk)


def fixture_wheel(destination, name="lark_oapi", version="1.9.0"):
    """An explicitly synthetic SDK wheel exercises real pip/hash/venv behavior."""
    destination.mkdir(exist_ok=True)
    info = f"{name}-{version}.dist-info"
    members = {
        f"{name}/__init__.py": b"async def aregister_app():\n    pass\n",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name.replace('_', '-')}\nVersion: {version}\n".encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = io.StringIO(newline="")
    writer = csv.writer(record)
    for member_name, content in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        writer.writerow((member_name, "sha256=" + digest, len(content)))
    writer.writerow((f"{info}/RECORD", "", ""))
    members[f"{info}/RECORD"] = record.getvalue().encode()
    wheel = destination / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as bundle:
        for member_name, content in members.items():
            bundle.writestr(member_name, content)
    return wheel


class SDKDependenciesTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tools = self.root / "tools"
        self.tools.mkdir(mode=0o700)
        self.wheelhouse = self.root / "wheelhouse"
        self.wheel = fixture_wheel(self.wheelhouse)
        self.lock = self.root / "requirements.lock"
        self.lock.write_text(f"lark-oapi==1.9.0 --hash=sha256:{sdk._hash(self.wheel)}\n")
        patcher = patch.object(sdk, "LOCK", self.lock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_lock_requires_exact_versions_and_sha256(self):
        for invalid in ("lark-oapi>=1.9.0\n", "lark-oapi==1.9.0\n",
                        "lark-oapi==1.9.0 --hash=md5:" + "a" * 32,
                        self.lock.read_text() * 2):
            with self.subTest(invalid=invalid):
                self.lock.write_text(invalid)
                with self.assertRaises(sdk.SDKError):
                    sdk._requirements()

    def test_invalid_wheel_refused_before_execution_or_venv(self):
        self.wheel.write_bytes(b"tampered")
        with patch.object(sdk, "_run") as run:
            with self.assertRaisesRegex(sdk.SDKError, "SHA256"):
                sdk.ensure_sdk(self.tools, self.wheelhouse)
            run.assert_not_called()
        self.assertFalse(list(self.tools.glob("python-*")))

    def test_extra_or_symlink_wheelhouse_file_is_refused(self):
        foreign = self.wheelhouse / "untrusted.txt"
        foreign.write_text("injected")
        with self.assertRaisesRegex(sdk.SDKError, "SHA256"):
            sdk.verify_wheelhouse(self.wheelhouse)
        foreign.unlink()
        foreign.symlink_to(self.wheel)
        with self.assertRaisesRegex(sdk.SDKError, "SHA256"):
            sdk.verify_wheelhouse(self.wheelhouse)

    def test_missing_one_locked_wheel_is_refused_before_execution(self):
        dependency = fixture_wheel(self.wheelhouse, "fixture_dependency", "1.0.0")
        self.lock.write_text(self.lock.read_text() +
                             f"fixture-dependency==1.0.0 --hash=sha256:{sdk._hash(dependency)}\n")
        self.assertEqual(sdk.verify_wheelhouse(self.wheelhouse), self.wheelhouse)
        dependency.unlink()
        with patch.object(sdk, "_run") as run:
            with self.assertRaisesRegex(sdk.SDKError, "缺少"):
                sdk.ensure_sdk(self.tools, self.wheelhouse)
            run.assert_not_called()
        self.assertFalse(list(self.tools.glob("python-*")))

    def test_compatibility_uses_noninstalling_offline_resolver(self):
        with patch.object(sdk, "_run") as run:
            self.assertEqual(sdk.check_wheelhouse_compatibility(self.wheelhouse), self.wheelhouse)
        command = run.call_args.args[0]
        for argument in ("--dry-run", "--ignore-installed", "--target", "--no-index", "--require-hashes"):
            self.assertIn(argument, command)

    def test_find_does_not_install_and_partial_environment_is_preserved(self):
        with patch.object(sdk, "_run") as run:
            self.assertIsNone(sdk.find_sdk(self.tools))
            directory = sdk._environment_path(self.tools, sdk._identity())
            directory.mkdir(mode=0o700)
            sentinel = directory / "keep"
            sentinel.write_text("existing")
            with self.assertRaisesRegex(sdk.SDKError, "不完整"):
                sdk.ensure_sdk(self.tools, self.wheelhouse)
            self.assertEqual(sentinel.read_text(), "existing")
            run.assert_not_called()

    def test_public_or_symlink_tools_are_refused(self):
        self.tools.chmod(0o755)
        with self.assertRaises(sdk.SDKError):
            sdk.ensure_sdk(self.tools, self.wheelhouse)
        self.tools.chmod(0o700)
        link = self.root / "link"
        link.symlink_to(self.tools, target_is_directory=True)
        with self.assertRaises(sdk.SDKError):
            sdk.ensure_sdk(link, self.wheelhouse)

    def test_pip_and_python_ambient_settings_are_removed(self):
        with patch.dict(os.environ, {"PIP_EXTRA_INDEX_URL": "https://example.invalid",
                                    "PIP_CONFIG_FILE": "hostile.conf", "PYTHONPATH": "/hostile",
                                    "LARKSUITE_CLI_APP_SECRET": "fixture-secret"}):
            environment = sdk._environment()
        self.assertNotIn("PIP_EXTRA_INDEX_URL", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("LARKSUITE_CLI_APP_SECRET", environment)
        self.assertEqual(environment["PIP_CONFIG_FILE"], os.devnull)

    @unittest.skipUnless(sys.version_info[:2] == (3, 12), "CPython 3.12 SDK contract")
    def test_real_offline_install_reuse_and_cache_tampering(self):
        # This invokes real venv and pip using only the synthetic local wheel.
        python = sdk.ensure_sdk(self.tools, self.wheelhouse)
        directory = python.parent.parent
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        pip_script = python.parent / "pip"
        self.assertIn(str(python), pip_script.read_text().splitlines()[0])
        with patch.object(sdk, "_run") as run:
            self.assertEqual(sdk.ensure_sdk(self.tools, self.wheelhouse), python)
            run.assert_not_called()
        receipt_path = directory / sdk.RECEIPT
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["distributions"]["lark-oapi"], "1.9.0")
        self.assertTrue(any(name.endswith(".dist-info/RECORD") for name in receipt["files"]))
        site = directory / "lib" / "python3.12" / "site-packages"
        for changed in (site / "lark_oapi" / "__init__.py",
                        site / "lark_oapi-1.9.0.dist-info" / "METADATA"):
            original = changed.read_bytes()
            changed.write_bytes(original + b"\n# changed\n")
            with patch.object(sdk, "_run") as run:
                with self.assertRaisesRegex(sdk.SDKError, "收据不匹配"):
                    sdk.ensure_sdk(self.tools, self.wheelhouse)
                run.assert_not_called()
            changed.write_bytes(original)
        injected = site / "injected.pth"
        injected.write_text("import os\n")
        with self.assertRaisesRegex(sdk.SDKError, "收据不匹配"):
            sdk.find_sdk(self.tools)
        injected.unlink()
        self.assertEqual(sdk.find_sdk(self.tools), python)
        original_receipt = receipt_path.read_text()
        receipt_path.write_text("[]")
        with self.assertRaisesRegex(sdk.SDKError, "收据不匹配"):
            sdk.find_sdk(self.tools)
        receipt_path.write_text(original_receipt)
        self.wheel.write_bytes(b"tampered explicit source")
        with self.assertRaisesRegex(sdk.SDKError, "SHA256"):
            sdk.ensure_sdk(self.tools, self.wheelhouse)

    def test_failed_creation_cleans_only_its_unpublished_directory(self):
        unrelated = self.tools / "other-deployment"
        unrelated.mkdir()
        sentinel = unrelated / "keep"
        sentinel.write_text("unchanged")
        with patch.object(sdk, "_run", side_effect=sdk.SDKError("fixture failure")):
            with self.assertRaisesRegex(sdk.SDKError, "fixture failure"):
                sdk.ensure_sdk(self.tools, self.wheelhouse)
        self.assertEqual(sentinel.read_text(), "unchanged")
        self.assertFalse(list(self.tools.glob("python-*")))


if __name__ == "__main__":
    unittest.main()
