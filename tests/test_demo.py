import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DemoAcceptance(unittest.TestCase):
    def test_documented_demo_command_runs_three_complete_conversations(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "demo.json"
            result = subprocess.run([sys.executable, "scripts/demo.py", "--output", str(output)],
                                    cwd=root, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text())
            self.assertTrue(report["passed"])
            self.assertEqual([row["recall"] for row in report["conversations"]], ["deleted", "deleted", "unsupported"])
            self.assertTrue(report["platform_http_requests"])
