"""Container supervisor: produce bounded evidence from two isolated pytest sessions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


PROTOCOL = "agentic-shortener-validation-v1"
MAX_ARTIFACT_BYTES = 768 * 1024


def fingerprint(root: Path) -> str:
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".json"}
    }
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def read_artifact(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError(f"Missing, oversized, or unsafe evidence artifact: {path.name}")
    return path.read_text()


def main() -> int:
    scenario, alias_min, alias_max = sys.argv[1:]
    trusted = Path("/trusted")
    expected = json.loads((trusted / "manifest.json").read_text())[scenario]
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONUNBUFFERED": "1", "HOME": "/tmp", "TMPDIR": "/tmp",
        "TEST_DATA_ROOT": "/tmp/test-data", "SCENARIO": scenario,
        "ALIAS_MIN_LENGTH": alias_min, "ALIAS_MAX_LENGTH": alias_max,
        "BASE_URL": "http://127.0.0.1:8000",
    }
    result = {"protocol": PROTOCOL, "evidence": {"trusted_fingerprint": fingerprint(trusted), "suites": {}}, "artifacts": {}}
    Path(env["TEST_DATA_ROOT"]).mkdir(mode=0o700)
    passed = True
    for kind, files in (
        ("protected", [str(trusted / file) for file in sorted({node.split("::")[0] for node in expected})]),
        ("generated", ["/candidate/generated_tests"]),
    ):
        report = Path(f"/tmp/{kind}.json")
        junit = Path(f"/tmp/{kind}.junit.xml")
        log = Path(f"/tmp/{kind}.log")
        with log.open("wb") as stream:
            process = subprocess.run(
                [sys.executable, "-I", "-B", "/runner/worker.py", kind, str(report), str(junit), *files],
                cwd="/trusted", env=env, stdout=stream, stderr=subprocess.STDOUT, check=False,
            )
        suite = json.loads(read_artifact(report))
        if process.returncode != suite["exit_code"]:
            raise ValueError(f"{kind} pytest exit code was inconsistent")
        result["evidence"]["suites"][kind] = suite
        result["artifacts"][f"{kind}.junit.xml"] = read_artifact(junit)
        result["artifacts"][f"{kind}.log"] = read_artifact(log)
        passed = passed and process.returncode == 0
    if passed:
        # Export through a separate process so candidate imports cannot mutate this supervisor.
        exported = subprocess.run(
            [sys.executable, "-I", "-B", "/runner/export_schema.py"],
            cwd="/trusted", env=env, capture_output=True, text=True, check=False,
        )
        if exported.returncode:
            raise ValueError(f"API/schema export failed: {exported.stderr[:2000]}")
        for filename in ("api.json", "schema.json"):
            result["artifacts"][filename] = read_artifact(Path("/tmp") / filename)
    if result["evidence"]["trusted_fingerprint"] != fingerprint(trusted):
        raise ValueError("Protected acceptance files changed during validation")
    result["evidence"]["summary"] = "Both suites passed" if passed else "Validation failed; inspect exact assertions and test logs"
    print(json.dumps(result, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
