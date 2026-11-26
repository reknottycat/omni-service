"""Linux deployment checks with isolated directories and fake systemd/providers."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from shutil import copyfile


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "linux" and os.getuid() != 0, "requires an unprivileged Linux user")
class PersonalInstallTest(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory(prefix="omni install % $ ")
        self.addCleanup(sandbox.cleanup)
        self.work = Path(sandbox.name)
        self.package = self.work / "package"
        self.sources = self.package / "src" / "omni_service"
        for directory in ("scripts", "deploy", "config", "src/omni_service"):
            (self.package / directory).mkdir(parents=True)
        for relative in ("scripts/install-personal.sh", "deploy/install_personal.py", "config/personal.example.json"):
            copyfile(ROOT / relative, self.package / relative)
        (self.sources / "personal_runner.py").write_text("# fixture\n")
        self.home = self.work / "home"
        self.home.mkdir(mode=0o700)
        self.config_home = self.home / ".config"
        self.data_home = self.home / ".local/share"
        self.config = self.config_home / "omni-service" / "team-a"
        self.data = self.data_home / "omni-service" / "team-a"
        self.unit = self.config_home / "systemd/user" / "omni-service-team-a.service"
        self.preflight_log = self.work / "preflight.json"
        self.systemctl_log = self.work / "systemctl.log"
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.env = {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.config_home),
            "XDG_DATA_HOME": str(self.data_home),
            "PATH": os.pathsep.join((str(self.bin), str(Path(sys.executable).parent), "/usr/bin", "/bin")),
            "PREFLIGHT_LOG": str(self.preflight_log), "SYSTEMCTL_LOG": str(self.systemctl_log),
            "FAILED_SYSTEMCTL": str(self.work / "failed-systemctl"),
            "LARKSUITE_CLI_TENANT_ACCESS_TOKEN": "synthetic-ambient-token",
        }
        (self.sources / "onboarding.py").write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "flag = 'LARKSUITE_CLI_TENANT_ACCESS_TOKEN'\n"
            "Path(os.environ['PREFLIGHT_LOG']).write_text(json.dumps({"
            "'argv': sys.argv, 'ambient_lark': flag in os.environ, 'cwd': os.getcwd(), "
            "'lark_config': os.environ['LARKSUITE_CLI_CONFIG_DIR'], "
            "'lark_data': os.environ['LARKSUITE_CLI_DATA_DIR']}))\n"
            "sys.exit(19 if os.environ.get('FAIL_PREFLIGHT') else 0)\n"
        )
        for name, script in {
            "lark-cli": "#!/bin/sh\nexit 0\n",
            "systemctl": (
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n"
                'if [ "$2" = "$FAIL_SYSTEMCTL" ] && [ ! -e "$FAILED_SYSTEMCTL" ]; then\n'
                '  touch "$FAILED_SYSTEMCTL"\n  exit 1\nfi\n'
            ),
        }.items():
            executable = self.bin / name
            executable.write_text(script)
            executable.chmod(0o700)

    def install(self, *arguments, **overrides):
        return subprocess.run(
            ["bash", str(self.package / "scripts/install-personal.sh"), "team-a", *arguments],
            env={**self.env, **overrides}, text=True, capture_output=True, check=False,
        )

    def test_staging_reuses_live_settings_and_db_without_starting(self):
        result = self.install("--no-start")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.config / "personal.json"
        credentials = self.config / "credentials.env"
        database = self.data / "bridge.sqlite3"
        initial_config = json.loads(config.read_text())
        self.assertEqual(initial_config["mode"], "disabled")
        self.assertRegex(credentials.read_text(), r"OMNI_ADMIN_SIGNING_KEY=[0-9a-f]{64}\n")
        for protected in (config, credentials, self.unit):
            self.assertEqual(stat.S_IMODE(protected.stat().st_mode), 0o600)
        self.assertIn("omni_service.onboarding", self.unit.read_text())
        self.assertIn("UnsetEnvironment=LARKSUITE_CLI_APP_ID", self.unit.read_text())
        self.assertNotIn("EnvironmentFile=", self.unit.read_text())
        self.assertNotIn("synthetic-ambient-token", self.unit.read_text())
        self.assertIn('%% $ ', self.unit.read_text())

        initial_config["mode"] = "personal-text-pilot"
        config.write_text(json.dumps(initial_config))
        saved = config.read_bytes(), credentials.read_bytes()
        database.write_bytes(b"existing conversation state")
        result = self.install("--no-start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((config.read_bytes(), credentials.read_bytes()), saved)
        self.assertEqual(database.read_bytes(), b"existing conversation state")
        self.assertEqual(len(list((self.data / "releases").iterdir())), 1)
        self.assertFalse(self.preflight_log.exists())
        self.assertFalse(self.systemctl_log.exists())

    def test_preflight_failure_preserves_previous_service(self):
        incoming = self.work / "first-config.json"
        incoming.write_text('{"mode":"personal-managed"}')
        secret_file = self.work / "incoming.env"
        secret_file.touch(mode=0o600)
        secret_file.write_text("TELEGRAM_BOT_TOKEN=9999:synthetic-secret\n")
        result = self.install("--config", str(incoming), "--secrets-file", str(secret_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        attempt = json.loads(self.preflight_log.read_text())
        self.assertIn("check", attempt["argv"])
        self.assertFalse(attempt["ambient_lark"])
        self.assertEqual(attempt["lark_config"], str(self.config / "lark"))
        self.assertEqual(attempt["lark_data"], str(self.config / "lark-data"))
        operations = self.systemctl_log.read_text()
        self.assertEqual(operations.splitlines(), [
            "--user daemon-reload", "--user restart omni-service-team-a.service",
            "--user is-active --quiet omni-service-team-a.service",
            "--user enable omni-service-team-a.service",
        ])
        existing_unit = self.unit.read_bytes()
        self.assertNotIn(b"synthetic-secret", existing_unit)
        (self.sources / "personal_runner.py").write_text("# staged change\n")
        result = self.install(FAIL_PREFLIGHT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.unit.read_bytes(), existing_unit)
        self.assertEqual(self.systemctl_log.read_text(), operations)

    def test_refuses_a_symlink_to_other_credentials(self):
        result = self.install("--no-start")
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = self.config / "credentials.env"
        other = self.config / "elsewhere"
        expected.rename(other)
        expected.symlink_to(other)
        result = self.install("--no-start")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(expected.is_symlink())
        self.assertTrue(other.is_file())

    def test_failed_activation_restores_exact_unit_and_restarts_old_version(self):
        incoming = self.work / "first-config.json"
        incoming.write_text('{"mode":"personal-managed"}')
        result = self.install("--config", str(incoming))
        self.assertEqual(result.returncode, 0, result.stderr)
        existing_unit = self.unit.read_bytes()
        (self.sources / "personal_runner.py").write_text("# new version\n")
        result = self.install("--no-start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.unit.read_bytes(), existing_unit)

        self.systemctl_log.unlink()
        result = self.install(FAIL_SYSTEMCTL="restart")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("已恢复先前的系统服务配置", result.stderr)
        self.assertEqual(self.unit.read_bytes(), existing_unit)
        self.assertEqual(self.systemctl_log.read_text().splitlines(), [
            "--user is-active --quiet omni-service-team-a.service",
            "--user daemon-reload", "--user restart omni-service-team-a.service",
            "--user daemon-reload", "--user restart omni-service-team-a.service",
            "--user is-active --quiet omni-service-team-a.service",
        ])
