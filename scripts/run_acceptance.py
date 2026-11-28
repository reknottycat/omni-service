#!/usr/bin/env python3
"""Run the full Linux mock contract and save evidence from the actual result."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def startTest(self, test):
        self.case_start = time.monotonic()
        self.case_status = "passed"
        super().startTest(test)

    def addFailure(self, test, err):
        self.case_status = "failed"
        super().addFailure(test, err)

    def addError(self, test, err):
        self.case_status = "error"
        super().addError(test, err)

    def addSkip(self, test, reason):
        self.case_status = "skipped"
        super().addSkip(test, reason)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.case_status = "failed"
        super().addSubTest(test, subtest, err)

    def stopTest(self, test):
        self.cases.append({"test": test.id(), "status": self.case_status,
                           "seconds": round(time.monotonic() - self.case_start, 6)})
        super().stopTest(test)


def source_manifest():
    files = []
    for folder in ("src", "tests", "scripts", "deploy", "config", "fixtures", ".github/workflows"):
        for path in sorted((ROOT / folder).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}:
                files.append({"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    for name in ("requirements-onboarding.txt", "requirements-onboarding.lock"):
        path = ROOT / name
        if path.is_file():
            files.append({"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return digest, files


def main():
    os.chdir(ROOT)
    if platform.system() != "Linux":
        print("This acceptance contract requires Linux.", file=sys.stderr)
        return 2
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
        print("CPython 3.12 with venv is required by the pinned onboarding contract.", file=sys.stderr)
        return 2
    required = {"test_channels.py", "test_language.py", "test_service.py", "test_http.py", "test_transport.py"}
    missing = sorted(name for name in required if not (ROOT / "tests" / name).is_file())
    if missing:
        print(f"Missing acceptance modules: {missing}", file=sys.stderr)
        return 2
    start = time.monotonic()
    digest_before, _files_before = source_manifest()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    commit = os.environ.get("OMNI_SOURCE_COMMIT")
    if not commit:
        found = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
        commit = found.stdout.strip() if found.returncode == 0 else "uncommitted"
    digest, files = source_manifest()
    source_consistent = digest_before == digest
    success = result.wasSuccessful() and result.testsRun > 0 and not result.skipped and source_consistent
    report = {
        "schema": 1, "scope": "P0-linux-mock", "mode": "simulation",
        "passed": success, "tests": result.testsRun,
        "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "environment": {"system": platform.system(), "release": platform.release(),
                        "machine": platform.machine(), "python": platform.python_version(),
                        "sqlite": sqlite3.sqlite_version},
        "source_commit": commit, "source_sha256": digest, "source_files": files,
        "source_consistent": source_consistent,
        "seconds": round(time.monotonic() - start, 3), "cases": result.cases,
        "limits": ["No live platform credentials or customer messages", "Deterministic translation and AI fixtures",
                   "No production capacity or model quality claim", "Media binary relay remains a separate milestone"]}
    output = ROOT / "artifacts"
    output.mkdir(exist_ok=True)
    (output / "acceptance.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("scope", "passed", "tests", "failures", "errors", "skipped", "source_sha256")}))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
