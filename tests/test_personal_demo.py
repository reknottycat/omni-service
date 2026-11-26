import json
from pathlib import Path
import subprocess
import sys
import unittest


class PersonalDemoAcceptance(unittest.TestCase):
    def test_public_demo_command_proves_end_to_end_observable_results(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, 'scripts/demo_personal.py'], cwd=root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report['passed'])
        self.assertEqual(0, report['external_network_calls'])
        self.assertTrue(all(report['checks'].values()))
        self.assertEqual(7, report['observed']['pending_staff_survived_restart'])
        self.assertEqual(1, report['observed']['telegram_deleted'])
        self.assertEqual(1, report['observed']['feishu_rooms'])
