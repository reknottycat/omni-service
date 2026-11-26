import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from omni_service.models import ChannelError


PATH = Path(__file__).resolve().parents[1] / "deploy" / "setup_personal.py"
SPEC = importlib.util.spec_from_file_location("setup_personal", PATH)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class SetupPersonalTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.dict(os.environ, {"XDG_CONFIG_HOME": directory.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = Path(directory.name) / "omni-service" / "support"
        self.tools = self.root / "tools"
        self.tools.mkdir(parents=True)
        self.py = self.tools / "sdk" / "bin" / "python"
        self.binary = self.tools / ("libtdjson.dylib" if sys.platform == "darwin" else "libtdjson.so")

    def test_prepare_does_not_invoke_account_entry(self):
        with (patch.object(setup, "_run") as run,
              patch("omni_service.onboarding_tools.ensure_lark_cli", return_value=self.tools) as lark,
              patch.object(setup, "ensure_sdk", return_value=self.py) as sdk,
              patch.object(setup, "ensure_tdlib", return_value=self.binary) as td,
              patch.object(setup.subprocess, "run") as external):
            self.assertEqual(setup.main(["support", "prepare"]), 0)
        self.assertEqual(run.call_args.args[0][-2:], ("support", "--no-start"))
        lark.assert_called_once_with(self.root)
        sdk.assert_called_once_with(self.tools)
        td.assert_called_once_with(self.tools, None)
        external.assert_not_called()

    def test_dependency_failure_stops_before_account_entry(self):
        with (patch.object(setup, "_run"),
              patch("omni_service.onboarding_tools.ensure_lark_cli",
                    side_effect=ChannelError("lark_cli_package_unverified")),
              patch.object(setup.subprocess, "run") as external):
            self.assertEqual(setup.main(["support", "configure"]), 1)
            external.assert_not_called()

    def test_configure_executes_private_interpreter_and_fixed_paths(self):
        with (patch.object(setup, "_run"),
              patch("omni_service.onboarding_tools.ensure_lark_cli", return_value=self.tools),
              patch.object(setup, "ensure_sdk", return_value=self.py),
              patch.object(setup, "ensure_tdlib", return_value=self.binary),
              patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 4)) as external):
            self.assertEqual(setup.main(["support", "configure", "--", "--telegram-owner", "@example"]), 4)
        command = external.call_args.args[0]
        self.assertEqual(command[0], self.py)
        self.assertEqual(command[6:9], ("configure", "--telegram-owner", "@example"))
        self.assertEqual(command[-4:], ("--config", self.root / "personal.json",
                                        "--secrets-file", self.root / "credentials.env"))

    def test_tampered_source_not_extracted_or_executed(self):
        source = self.root / "wrong.tar.gz"
        source.write_bytes(b"invalid")
        with (patch.object(setup.tarfile, "open") as unpack, patch.object(setup, "_run") as run):
            with self.assertRaisesRegex(setup.SetupError, "校验不匹配"):
                setup.ensure_tdlib(self.tools, source)
        unpack.assert_not_called()
        run.assert_not_called()

    def test_tampered_cached_binary_not_probed(self):
        receipt = self.tools / "tdlib-install.json"
        receipt.write_text(json.dumps({"commit": setup.TD_REV, "platform": sys.platform,
                                       "arch": setup.platform.machine(), "sha256": "a" * 64}))
        self.binary.write_bytes(b"invalid")
        receipt.chmod(0o600)
        self.binary.chmod(0o700)
        with patch.object(setup, "_offline_probe") as probe:
            with self.assertRaisesRegex(setup.SetupError, "安装收据不匹配"):
                setup.ensure_tdlib(self.tools)
            probe.assert_not_called()
