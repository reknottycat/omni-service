"""Real archive parsing with synthetic, explicitly pinned component bytes.

These tests do not load native code, install a wheel, or authorize an account.
"""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from omni_service import onboarding_tools

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))
SPEC = importlib.util.spec_from_file_location("dependency_bundle_contract", DEPLOY / "dependency_bundle.py")
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)
import sdk_dependencies


def sha(data):
    return hashlib.sha256(data).hexdigest()


class DependencyBundleContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()
        self.source = b"synthetic pinned TDLib source; never extracted or built"
        self.cli = b"synthetic pinned CLI archive; never executed"
        self.wheel = b"synthetic pinned wheel; never installed"
        self.lock = f"lark-oapi==1.9.0 --hash=sha256:{sha(self.wheel)}\n".encode()
        self.lock_path = self.root / "requirements-onboarding.lock"
        self.lock_path.write_bytes(self.lock)
        self.files = {
            "tdlib-source.tar.gz": self.source,
            "requirements-onboarding.lock": self.lock,
            "lark-cli-1.0.99-linux-amd64.tar.gz": self.cli,
            "wheelhouse/lark_oapi-1.9.0-py3-none-any.whl": self.wheel,
        }
        for patcher in (
                patch.object(bundle, "ROOT", self.root),
                patch.object(bundle, "TD_SHA", sha(self.source)),
                patch.object(bundle, "platform_key", return_value="linux-amd64"),
                patch.dict(onboarding_tools._SHA256, {("linux", "amd64"): sha(self.cli)}),
                patch.object(sdk_dependencies, "LOCK", self.lock_path)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manifest = {**bundle.expected_identity(), "kind": "source"}

    def manifest_bytes(self, files=None, *, changes=None):
        files = self.files if files is None else files
        manifest = {**self.manifest, "files": {
            name: {"sha256": sha(data), "size": len(data)} for name, data in files.items()}}
        if changes:
            manifest.update(changes)
        return json.dumps(manifest, sort_keys=True).encode()

    def make_tar(self, files=None, *, manifest=None, extra_entries=()):
        files = self.files if files is None else files
        target = self.root / "dependencies.tar.gz"
        with tarfile.open(target, "w:gz") as output:
            payloads = {**files, "manifest.json": self.manifest_bytes(files) if manifest is None else manifest}
            for name, data in payloads.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                output.addfile(entry, io.BytesIO(data))
            for entry, payload in extra_entries:
                output.addfile(entry, io.BytesIO(payload) if payload is not None else None)
        return target, sha(target.read_bytes())

    def assert_rejected(self, archive, digest, message=None):
        context = self.assertRaisesRegex(bundle.BundleError, message) if message else self.assertRaises(bundle.BundleError)
        with context:
            with bundle.unpack_bundle(archive, digest):
                self.fail("Invalid dependency archive was exposed to an installer")

    def test_valid_source_archive_is_verified_then_cleaned_up(self):
        archive, digest = self.make_tar()
        with bundle.unpack_bundle(archive, digest) as (extracted, manifest):
            self.assertEqual(manifest["kind"], "source")
            self.assertEqual(manifest["platform"], "linux-amd64")
            self.assertEqual((extracted / "tdlib-source.tar.gz").read_bytes(), self.source)
        self.assertFalse(extracted.exists())

    def test_valid_native_archive_retains_native_payload_identity(self):
        files = {**self.files, "native/libtdjson.so": b"synthetic native library; never loaded"}
        archive, digest = self.make_tar(files, manifest=self.manifest_bytes(files, changes={"kind": "native"}))
        with bundle.unpack_bundle(archive, digest) as (extracted, manifest):
            self.assertEqual(manifest["kind"], "native")
            self.assertEqual(sha((extracted / "native/libtdjson.so").read_bytes()),
                             manifest["files"]["native/libtdjson.so"]["sha256"])

    def test_wrong_outer_digest_is_rejected_before_tar_open(self):
        archive, _ = self.make_tar()
        with patch.object(bundle.tarfile, "open") as unpack:
            self.assert_rejected(archive, "0" * 64, "SHA256")
        unpack.assert_not_called()

    def test_replacing_input_after_verification_cannot_replace_native_payload(self):
        trusted = {**self.files, "native/libtdjson.so": b"trusted native fixture"}
        archive, digest = self.make_tar(trusted, manifest=self.manifest_bytes(trusted, changes={"kind": "native"}))
        trusted_archive = archive.read_bytes()
        substituted = {**self.files, "native/libtdjson.so": b"untrusted replacement"}
        self.make_tar(substituted, manifest=self.manifest_bytes(substituted, changes={"kind": "native"}))
        substituted_archive = archive.read_bytes()
        archive.write_bytes(trusted_archive)
        original_open = tarfile.open

        def replace_original_before_extraction(*args, **kwargs):
            archive.write_bytes(substituted_archive)
            return original_open(*args, **kwargs)

        with patch.object(bundle.tarfile, "open", side_effect=replace_original_before_extraction):
            with bundle.unpack_bundle(archive, digest) as (extracted, _):
                self.assertEqual((extracted / "native/libtdjson.so").read_bytes(), trusted["native/libtdjson.so"])

    def test_missing_malformed_and_noncanonical_outer_digest_are_rejected(self):
        archive, digest = self.make_tar()
        for value in (None, "", "not-a-digest", digest.upper()):
            with self.subTest(digest=value):
                self.assert_rejected(archive, value)

    def test_wrong_architecture_python_lock_or_source_pin_is_rejected(self):
        variants = {
            "architecture": {"platform": "linux-arm64"},
            "python": {"python": "cpython-3.13"},
            "lock": {"sdk_lock_sha256": "1" * 64},
            "source": {"tdlib": {**self.manifest["tdlib"], "source_sha256": "1" * 64}},
            "commit": {"tdlib": {**self.manifest["tdlib"], "commit": "0" * 40}},
            "cli": {"lark_cli": {**self.manifest["lark_cli"], "version": "0.0.1"}},
        }
        for label, changes in variants.items():
            with self.subTest(variant=label):
                archive, digest = self.make_tar(manifest=self.manifest_bytes(changes=changes))
                self.assert_rejected(archive, digest, "固定版本")

    def test_actual_source_and_lock_cannot_be_rehashed_into_manifest(self):
        for name in ("tdlib-source.tar.gz", "requirements-onboarding.lock"):
            with self.subTest(path=name):
                files = {**self.files, name: b"replacement bytes with a consistent self-reported hash"}
                archive, digest = self.make_tar(files)
                self.assert_rejected(archive, digest, "摘要|锁文件")

    def test_actual_cli_cannot_be_rehashed_into_manifest(self):
        files = {**self.files, "lark-cli-1.0.99-linux-amd64.tar.gz": b"replacement CLI"}
        archive, digest = self.make_tar(files)
        self.assert_rejected(archive, digest)

    def test_actual_wheel_cannot_be_rehashed_into_manifest(self):
        files = {**self.files, "wheelhouse/lark_oapi-1.9.0-py3-none-any.whl": b"replacement wheel"}
        archive, digest = self.make_tar(files)
        self.assert_rejected(archive, digest)

    def test_path_traversal_absolute_and_ambiguous_paths_are_rejected(self):
        for path in ("../outside", "/tmp/omni-escaped", "a/../../outside", "a\\outside", "./x", "a//x"):
            with self.subTest(path=path):
                entry = tarfile.TarInfo(path)
                entry.size = 1
                archive, digest = self.make_tar(extra_entries=((entry, b"x"),))
                self.assert_rejected(archive, digest, "唯一普通文件")

    def test_links_devices_and_explicit_directory_entries_are_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE, tarfile.DIRTYPE):
            with self.subTest(kind=kind):
                entry = tarfile.TarInfo("untrusted-entry")
                entry.type = kind
                entry.linkname = "tdlib-source.tar.gz"
                archive, digest = self.make_tar(extra_entries=((entry, None),))
                self.assert_rejected(archive, digest, "唯一普通文件")

    def test_duplicate_archive_entry_is_rejected(self):
        entry = tarfile.TarInfo("tdlib-source.tar.gz")
        entry.size = len(self.source)
        archive, digest = self.make_tar(extra_entries=((entry, self.source),))
        self.assert_rejected(archive, digest, "唯一普通文件")

    def test_unlisted_extra_file_is_rejected(self):
        files = {**self.files, "extra.txt": b"not listed"}
        archive, digest = self.make_tar(files, manifest=self.manifest_bytes())
        self.assert_rejected(archive, digest, "清单不完整")

    def test_missing_manifest_file_is_rejected(self):
        files = dict(self.files)
        files.pop("tdlib-source.tar.gz")
        archive, digest = self.make_tar(files, manifest=self.manifest_bytes())
        self.assert_rejected(archive, digest, "清单不完整")

    def test_required_source_and_wheelhouse_cannot_be_omitted_from_both(self):
        for omitted in ("tdlib-source.tar.gz", "wheelhouse/lark_oapi-1.9.0-py3-none-any.whl"):
            with self.subTest(path=omitted):
                files = dict(self.files)
                files.pop(omitted)
                archive, digest = self.make_tar(files)
                self.assert_rejected(archive, digest, "缺少")

    def test_wrong_file_digest_or_size_is_rejected(self):
        for field, value in (("sha256", "1" * 64), ("size", 0)):
            with self.subTest(field=field):
                manifest = json.loads(self.manifest_bytes())
                manifest["files"]["tdlib-source.tar.gz"][field] = value
                archive, digest = self.make_tar(manifest=json.dumps(manifest).encode())
                self.assert_rejected(archive, digest, "内部文件摘要")

    def test_non_object_or_missing_manifest_fields_are_handled_as_bundle_error(self):
        for raw in (b"[]", b"null", b"42", b"{}", b"{invalid"):
            with self.subTest(manifest=raw):
                archive, digest = self.make_tar(manifest=raw)
                self.assert_rejected(archive, digest)

    def test_native_kind_must_match_payload(self):
        archive, digest = self.make_tar(manifest=self.manifest_bytes(changes={"kind": "native"}))
        self.assert_rejected(archive, digest, "类型")

    def test_total_expanded_size_limit_precedes_installation(self):
        archive, digest = self.make_tar()
        with patch.object(bundle, "MAX_EXPANDED", 10):
            self.assert_rejected(archive, digest, "大小上限")

    def test_archive_symlink_is_rejected(self):
        archive, digest = self.make_tar()
        alias = self.root / "alias.tar.gz"
        alias.symlink_to(archive)
        self.assert_rejected(alias, digest)


if __name__ == "__main__":
    unittest.main()
