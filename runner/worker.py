"""Trusted pytest entry point; candidates cannot supply plugins or configuration."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


class Evidence:
    def __init__(self, protected: bool):
        self.protected = protected
        self.collected: list[str] = []
        self.tests: dict = {}
        self.collection_errors: list[str] = []

    def node_id(self, node_id: str) -> str:
        if self.protected:
            return node_id.removeprefix("/trusted/")
        return node_id

    def pytest_collection_finish(self, session):
        self.collected = [self.node_id(item.nodeid) for item in session.items]
        self.tests = {node_id: {} for node_id in self.collected}

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(str(report.longrepr))

    def pytest_runtest_logreport(self, report):
        phase = {"outcome": report.outcome, "duration": report.duration,
                 "wasxfail": getattr(report, "wasxfail", None)}
        if report.longrepr:
            phase["longrepr"] = str(report.longrepr)
        self.tests.setdefault(self.node_id(report.nodeid), {})[report.when] = phase


def run_suite(kind: str, report_path: str, junit_path: str, files: list[str], *,
              service: Path = Path("/candidate/service"), trusted: Path = Path("/trusted"),
              temporary: Path = Path("/tmp")) -> int:
    sys.path.insert(0, str(service))
    sys.path.insert(0, str(trusted))
    evidence = Evidence(kind == "protected")
    exit_code = int(pytest.main([
        "-c", str(Path(__file__).with_name("pytest.ini")), f"--rootdir={trusted}", f"--confcutdir={trusted}", "--noconftest",
        "-p", "no:cacheprovider", "--basetemp", str(temporary / f"{kind}-pytest"), "--junitxml", junit_path,
        "--strict-markers", "--strict-config", "--tb=short", "-q", *files,
    ], plugins=[evidence]))
    Path(report_path).write_text(json.dumps({
        "collected": evidence.collected, "tests": evidence.tests,
        "collection_errors": evidence.collection_errors, "exit_code": exit_code,
    }))
    return exit_code


def main() -> int:
    kind, report_path, junit_path, *files = sys.argv[1:]
    return run_suite(kind, report_path, junit_path, files)


if __name__ == "__main__":
    raise SystemExit(main())
