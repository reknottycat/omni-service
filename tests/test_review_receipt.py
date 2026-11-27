import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_review_receipt.py"
SPEC = importlib.util.spec_from_file_location("check_review_receipt", SCRIPT)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class ReviewReceiptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.receipt = self.root / "receipt.json"
        self.env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
        self.git("init", "-q")
        (self.root / "REVIEW.md").write_text("trusted base rules\n")
        self.git("add", "REVIEW.md")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-qb", "candidate")
        (self.root / "REVIEW.md").write_text("untrusted candidate rules\n")
        self.git("add", "REVIEW.md")
        self.git("commit", "-qm", "candidate")
        self.head = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "--detach", self.base)
        self.git("merge", "--no-ff", "-qm", "combine", self.head)
        self.tested = self.git("rev-parse", "HEAD")
        self.payload = {
            "head": self.head, "base": self.base, "tested_commit": self.tested,
            "tested_tree": self.git("rev-parse", "HEAD^{tree}"),
            "policy_sha256": hashlib.sha256(b"trusted base rules\n").hexdigest(),
            "decision": "pass", "evidence": ["receipt/acceptance.json"],
        }

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def run_check(self, mode, *, head=None, base=None, tested=None):
        completed = subprocess.run([
            sys.executable, str(SCRIPT), "--mode", mode, "--receipt", str(self.receipt),
            "--head", head or self.head, "--base", base or self.base, "--tested", tested or self.tested,
        ], cwd=self.root, env=self.env, capture_output=True, text=True, check=False)
        return completed.returncode, json.loads(completed.stdout)["result"]

    def save(self, **patch):
        self.receipt.write_text(json.dumps({**self.payload, **patch}))

    def test_modes_for_missing_and_blocking_receipts(self):
        self.assertEqual(self.run_check("off"), (0, "off"))
        self.assertEqual(self.run_check("observe"), (0, "invalid"))
        self.assertEqual(self.run_check("enforce"), (1, "invalid"))
        self.save(decision="block")
        self.assertEqual(self.run_check("observe"), (0, "block"))
        self.assertEqual(self.run_check("enforce"), (1, "block"))

    def test_valid_receipt_is_bound_to_target_policy_and_tested_tree(self):
        self.save()
        self.assertEqual(self.run_check("enforce"), (0, "pass"))
        candidate_policy = hashlib.sha256(b"untrusted candidate rules\n").hexdigest()
        for patch in ({"policy_sha256": candidate_policy}, {"tested_tree": "0" * 40}, {"evidence": []}):
            with self.subTest(patch=patch):
                self.save(**patch)
                self.assertEqual(self.run_check("enforce"), (1, "invalid"))

    def test_head_base_or_non_merge_candidate_does_not_pass(self):
        self.save()
        self.assertEqual(self.run_check("enforce", head=self.base), (1, "invalid"))
        self.assertEqual(self.run_check("enforce", base=self.head), (1, "invalid"))
        self.save(tested_commit=self.head, tested_tree=self.git("rev-parse", self.head + "^{tree}"))
        self.assertEqual(self.run_check("enforce", tested=self.head), (1, "invalid"))

    def test_duplicate_or_oversized_receipts_fail_closed(self):
        self.receipt.write_text('{"decision":"pass","decision":"block"}')
        self.assertEqual(self.run_check("enforce"), (1, "invalid"))
        self.receipt.write_text(" " * 65537)
        self.assertEqual(self.run_check("enforce"), (1, "invalid"))
