"""Digest, native identity, offline installation and non-overwrite contracts."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from omni_service import onboarding_tools as tools
from omni_service.models import ChannelError


def elf(machine=62):
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header) + b"synthetic-test-file-not-executable"


def archive(path, contents, *, duplicate=False):
    with tarfile.open(path, "w:gz") as bundle:
        for _ in range(2 if duplicate else 1):
            entry = tarfile.TarInfo("lark-cli")
            entry.size = len(contents)
            bundle.addfile(entry, io.BytesIO(contents))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LarkToolInstallation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.team = self.root / "team"
        self.team.mkdir(mode=0o700)
        self.archive = self.root / "official-fixture.tar.gz"
        self.payload = elf()
        self.digest = archive(self.archive, self.payload)
        self.mock_platform = patch.object(tools.platform, "system", return_value="Linux")
        self.mock_machine = patch.object(tools.platform, "machine", return_value="x86_64")
        self.mock_digest = patch.dict(tools._SHA256, {("linux", "amd64"): self.digest})
        for patcher in (self.mock_platform, self.mock_machine, self.mock_digest):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.version = patch.object(tools.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, b"lark-cli version 1.0.99\n", b""))
        self.run = self.version.start()
        self.addCleanup(self.version.stop)
        self.download = patch.object(tools, "_download_archive", side_effect=AssertionError("network forbidden"))
        self.network = self.download.start()
        self.addCleanup(self.download.stop)

    def install(self):
        return tools.ensure_lark_cli(self.team, archive=self.archive)

    def test_offline_install_and_repeat_preserve_existing_binary(self):
        directory = self.install()
        binary = directory / "lark-cli"
        state = (binary.stat().st_ino, binary.stat().st_mtime_ns, binary.read_bytes())
        receipt = json.loads((directory / "lark-cli-install.json").read_text())
        self.assertEqual(receipt["archive_sha256"], self.digest)
        self.assertEqual(receipt["binary_sha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(receipt["arch"], "amd64")
        self.assertEqual(binary.stat().st_mode & 0o777, 0o700)
        tools.ensure_lark_cli(self.team)
        self.assertEqual(state, (binary.stat().st_ino, binary.stat().st_mtime_ns, binary.read_bytes()))
        self.network.assert_not_called()
        self.assertEqual(self.run.call_count, 2)
        self.assertNotIn("LARKSUITE_CLI_APP_SECRET", self.run.call_args.kwargs["env"])
        self.assertEqual(self.run.call_args.args[0][1], "--version")

    def test_corrupt_archive_never_executes_or_installs(self):
        self.archive.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ChannelError, "lark_cli_package_unverified"):
            self.install()
        self.run.assert_not_called()
        self.assertFalse((self.team / "tools" / "lark-cli").exists())

    def test_cached_archive_tampering_is_rejected_without_repair(self):
        directory = self.install()
        cached = directory / "lark-cli-1.0.99-linux-amd64.tar.gz"
        cached.write_bytes(b"changed")
        binary = directory / "lark-cli"
        original = binary.read_bytes()
        self.run.reset_mock()
        with self.assertRaisesRegex(ChannelError, "lark_cli_package_unverified"):
            self.install()
        self.run.assert_not_called()
        self.assertEqual(cached.read_bytes(), b"changed")
        self.assertEqual(binary.read_bytes(), original)

    def test_changed_installed_binary_is_preserved_and_never_executed(self):
        directory = self.install()
        binary = directory / "lark-cli"
        binary.write_bytes(b"existing-deployment")
        self.run.reset_mock()
        with self.assertRaisesRegex(ChannelError, "lark_cli_package_unverified"):
            self.install()
        self.run.assert_not_called()
        self.assertEqual(binary.read_bytes(), b"existing-deployment")

    def test_earlier_verified_binary_is_adopted_without_overwrite(self):
        directory = self.team / "tools"
        directory.mkdir(mode=0o700)
        binary = directory / "lark-cli"
        binary.write_bytes(self.payload)
        binary.chmod(0o700)
        inode = binary.stat().st_ino
        self.install()
        self.assertEqual(binary.stat().st_ino, inode)
        self.assertTrue((directory / "lark-cli-install.json").is_file())

    def test_receipt_tampering_is_rejected(self):
        directory = self.install()
        receipt = directory / "lark-cli-install.json"
        incoming = json.loads(receipt.read_text())
        incoming["version"] = "0.0.1"
        receipt.write_text(json.dumps(incoming))
        self.run.reset_mock()
        with self.assertRaisesRegex(ChannelError, "lark_cli_package_unverified"):
            self.install()
        self.run.assert_not_called()
        self.assertEqual(json.loads(receipt.read_text())["version"], "0.0.1")

    def test_symlink_binary_is_not_followed_or_replaced(self):
        directory = self.team / "tools"
        directory.mkdir(mode=0o700)
        victim = self.root / "other-team-cli"
        victim.write_bytes(b"other-team")
        (directory / "lark-cli").symlink_to(victim)
        with self.assertRaises(ChannelError):
            self.install()
        self.run.assert_not_called()
        self.assertTrue((directory / "lark-cli").is_symlink())
        self.assertEqual(victim.read_bytes(), b"other-team")

    def test_supplied_archive_symlink_is_rejected(self):
        alias = self.root / "alias.tar.gz"
        alias.symlink_to(self.archive)
        with self.assertRaises(ChannelError):
            tools.ensure_lark_cli(self.team, archive=alias)
        self.run.assert_not_called()

    def test_wrong_architecture_rejected_before_execution(self):
        digest = archive(self.archive, elf(machine=183))
        with patch.dict(tools._SHA256, {("linux", "amd64"): digest}):
            with self.assertRaisesRegex(ChannelError, "lark_cli_package_unverified"):
                self.install()
        self.run.assert_not_called()

    def test_native_version_mismatch_does_not_publish_binary(self):
        self.run.return_value = subprocess.CompletedProcess([], 0, b"lark-cli version 1.0.100\n", b"")
        with self.assertRaisesRegex(ChannelError, "lark_cli_package_unverified"):
            self.install()
        self.assertFalse((self.team / "tools" / "lark-cli").exists())
        self.assertFalse((self.team / "tools" / "lark-cli-install.json").exists())

    def test_duplicate_executable_in_archive_is_rejected(self):
        digest = archive(self.archive, self.payload, duplicate=True)
        with patch.dict(tools._SHA256, {("linux", "amd64"): digest}):
            with self.assertRaisesRegex(ChannelError, "lark_cli_package_unverified"):
                self.install()
        self.run.assert_not_called()

    def test_world_readable_cache_is_rejected(self):
        directory = self.install()
        cached = directory / "lark-cli-1.0.99-linux-amd64.tar.gz"
        cached.chmod(0o644)
        self.run.reset_mock()
        with self.assertRaisesRegex(ChannelError, "lark_cli_package_unverified"):
            tools.ensure_lark_cli(self.team)
        self.run.assert_not_called()

    def test_unsupported_platform_does_not_create_tools(self):
        with patch.object(tools.platform, "machine", return_value="riscv64"):
            with self.assertRaisesRegex(ChannelError, "lark_cli_platform_unsupported"):
                self.install()
        self.assertFalse((self.team / "tools").exists())


if __name__ == "__main__":
    unittest.main()
