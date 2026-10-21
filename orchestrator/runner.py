"""Fixed-command, fail-closed Docker validation for immutable candidates."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import time
from typing import Callable
import uuid
import xml.etree.ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "agentic-shortener-validation-v1"
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 16 * 1024
SCENARIOS = {"greenfield", "brownfield", "ambiguous"}


class RunnerUnavailable(RuntimeError):
    """The required isolated execution environment is unavailable."""


def _checked_directory(path: Path) -> Path:
    path = Path(path).absolute()
    if not path.is_dir() or path.is_symlink() or path.resolve() != path:
        raise ValueError(f"Expected a real directory without symlink parents: {path}")
    if any(char in str(path) for char in (",", "\n", "\r")):
        raise ValueError("Mount paths must not contain commas or newlines")
    for child in path.rglob("*"):
        if child.is_symlink() or not (child.is_dir() or child.is_file()):
            raise ValueError(f"Unsupported candidate entry: {child}")
    return path


def _trusted_fingerprint(root: Path) -> str:
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".json"}
    }
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _rejected_diagnostics(envelope: object) -> dict:
    """Keep bounded repair context without treating rejected evidence as authoritative."""
    result = {"trusted_for_acceptance": False, "truncated": False,
              "notice": "Untrusted diagnostic excerpts from rejected evidence; they cannot establish a passing result.",
              "collection_errors": {}, "logs": {}}
    remaining = MAX_DIAGNOSTIC_BYTES

    def excerpt(value: object) -> str:
        nonlocal remaining
        if not isinstance(value, str):
            return ""
        encoded = value.encode("utf-8", errors="replace")
        result["truncated"] = result["truncated"] or len(encoded) > remaining
        selected = encoded[:remaining]
        remaining -= len(selected)
        return selected.decode("utf-8", errors="ignore")

    if not isinstance(envelope, dict):
        return result
    evidence, artifacts = envelope.get("evidence", {}), envelope.get("artifacts", {})
    suites = evidence.get("suites", {}) if isinstance(evidence, dict) else {}
    for name in ("protected", "generated"):
        suite = suites.get(name, {}) if isinstance(suites, dict) else {}
        errors = suite.get("collection_errors", []) if isinstance(suite, dict) else []
        if isinstance(errors, list):
            result["truncated"] = result["truncated"] or len(errors) > 8
            result["collection_errors"][name] = [text for error in errors[:8] if (text := excerpt(error))]
    for name in ("protected.log", "generated.log"):
        if isinstance(artifacts, dict) and (text := excerpt(artifacts.get(name))):
            result["logs"][name] = text
    return result


class DockerRunner:
    mode = "local"

    def __init__(self, image: str = "agentic-shortener-runner:local", timeout: int = 120, *, mode: str = "local"):
        if timeout <= 0:
            raise ValueError("Validation timeout must be positive")
        if mode != "local":
            raise ValueError("Docker execution mode must be local")
        self.mode = mode
        self.image = image
        self.timeout = timeout
        self.acceptance = PROJECT_ROOT / "acceptance"
        self.harness = PROJECT_ROOT / "runner"
        self.docker: str | None = None

    def preflight(self) -> dict:
        self.docker = shutil.which("docker")
        if not self.docker:
            raise RunnerUnavailable("Docker CLI is unavailable; install and start Docker. Host execution is disabled.")
        try:
            info = self._command("info", "--format", "{{.ServerVersion}}")
            if info.returncode or not info.stdout.strip():
                raise RunnerUnavailable(f"Docker daemon is unavailable: {info.stderr.strip()[:500]}")
            image = self._command("image", "inspect", "--format", "{{.Id}}", self.image)
            image_id = image.stdout.strip()
            if image.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise RunnerUnavailable(
                    f"Prepared runner image {self.image!r} is unavailable. "
                    "Build it with: docker build -f runner/Dockerfile -t agentic-shortener-runner:local ."
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RunnerUnavailable(f"Docker preflight failed: {exc}") from exc
        return {"available": True, "mode": self.mode, "image": self.image, "image_id": image_id, "engine_version": info.stdout.strip()}

    def _command(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([self.docker or "docker", *args], capture_output=True, text=True, timeout=15, check=False)

    def _expected(self, scenario: str) -> list[str]:
        _checked_directory(self.acceptance)
        manifest = json.loads((self.acceptance / "manifest.json").read_text())
        expected = manifest.get(scenario)
        if not isinstance(expected, list) or not expected or len(set(expected)) != len(expected):
            raise ValueError(f"Invalid protected acceptance manifest for {scenario}")
        for node_id in expected:
            if not isinstance(node_id, str) or "::" not in node_id:
                raise ValueError("Protected manifest must contain pytest node IDs")
            filename = node_id.split("::", 1)[0]
            if Path(filename).name != filename or not filename.startswith("test_") or not filename.endswith(".py"):
                raise ValueError("Protected manifest filenames must be top-level test_*.py files")
            if not (self.acceptance / filename).is_file():
                raise ValueError(f"Protected acceptance file is missing: {filename}")
        return expected

    def _create_command(self, name: str, token: str, image_id: str, candidate: Path,
                        scenario: str, alias_min: int, alias_max: int) -> list[str]:
        command = [
            self.docker or "docker", "create", "--name", name,
            "--label", f"agentic-shortener.validation={token}",
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--cpus", "1",
            "--memory", "512m", "--memory-swap", "512m", "--pids-limit", "64",
            "--ipc", "none", "--user", f"{os.getuid() or 1000}:{os.getgid() or 1000}",
            "--tmpfs", "/tmp:rw,nosuid,noexec,size=128m,mode=1777",
            "--workdir", "/trusted", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "--env", "PYTHONUNBUFFERED=1",
        ]
        for source, target in (
            (candidate / "service", "/candidate/service"),
            (candidate / "generated_tests", "/candidate/generated_tests"),
            (self.acceptance, "/trusted"), (self.harness, "/runner"),
        ):
            command.extend(["--mount", f"type=bind,source={source},target={target},readonly"])
        command.extend([image_id, "python", "-I", "-B", "/runner/harness.py", scenario, str(alias_min), str(alias_max)])
        return command

    def _kill_owned(self, name: str, token: str) -> None:
        """Check ownership before killing; retain the stopped container for inspection."""
        result = self._command("inspect", "--format", '{{index .Config.Labels "agentic-shortener.validation"}}', name)
        if result.returncode == 0 and result.stdout.strip() == token:
            self._command("kill", name)

    def _capture(self, name: str, token: str, stop_requested: Callable[[], bool] | None) -> dict:
        if stop_requested and stop_requested():
            return {"stdout": b"", "stderr": b"", "cli_exit_code": 0, "termination": "stopped"}
        started = time.monotonic()
        process = subprocess.Popen(
            [self.docker or "docker", "start", "--attach", name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        termination = None
        terminated_at = None
        selector = selectors.DefaultSelector()
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        try:
            while selector.get_map():
                if termination is None:
                    if stop_requested and stop_requested():
                        termination = "stopped"
                    elif time.monotonic() - started >= self.timeout:
                        termination = "timeout"
                    if termination:
                        self._kill_owned(name, token)
                        process.terminate()
                        terminated_at = time.monotonic()
                for key, _ in selector.select(timeout=0.1):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = MAX_OUTPUT_BYTES - len(captured[key.data])
                    captured[key.data].extend(chunk[:available])
                    if len(chunk) > available and termination is None:
                        termination = "output_limit"
                        self._kill_owned(name, token)
                        process.terminate()
                        terminated_at = time.monotonic()
                if terminated_at is not None and time.monotonic() - terminated_at >= 5:
                    process.kill()
                if termination and process.poll() is not None:
                    break
            try:
                cli_exit = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill_owned(name, token)
                process.kill()
                cli_exit = process.wait(timeout=5)
                termination = termination or "attach_timeout"
        except BaseException:
            self._kill_owned(name, token)
            process.kill()
            process.wait(timeout=5)
            raise
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()
        return {"stdout": bytes(captured["stdout"]), "stderr": bytes(captured["stderr"]),
                "cli_exit_code": cli_exit, "termination": termination}

    @staticmethod
    def _assess(envelope: dict, expected: list[str], fingerprint: str, exit_code: int) -> tuple[dict, dict]:
        if not isinstance(envelope, dict) or envelope.get("protocol") != PROTOCOL:
            raise ValueError("Missing trusted runner protocol")
        evidence = envelope.get("evidence")
        artifacts = envelope.get("artifacts")
        if not isinstance(evidence, dict) or not isinstance(artifacts, dict):
            raise ValueError("Incomplete runner evidence")
        if evidence.get("trusted_fingerprint") != fingerprint:
            raise ValueError("Protected acceptance fingerprint differs from the host snapshot")
        suites = evidence.get("suites")
        if not isinstance(suites, dict) or set(suites) != {"protected", "generated"}:
            raise ValueError("Missing protected or generated suite evidence")
        protected = suites["protected"]
        if not isinstance(protected, dict) or sorted(protected.get("collected", [])) != sorted(expected):
            raise ValueError("Mandatory protected test IDs were missing, duplicated, or changed")
        counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        outcomes = []
        for name, suite in suites.items():
            collected = suite.get("collected", [])
            tests = suite.get("tests", {})
            if not isinstance(collected, list) or not isinstance(tests, dict) or len(set(collected)) != len(collected):
                raise ValueError(f"Malformed {name} suite report")
            if sorted(tests) != sorted(collected):
                raise ValueError(f"Incomplete {name} test outcomes")
            if suite.get("collection_errors"):
                counts["errors"] += len(suite["collection_errors"])
            if not collected and name == "generated":
                raise ValueError("No generated tests were collected")
            for node_id, phases in tests.items():
                if not isinstance(phases, dict):
                    raise ValueError(f"Malformed outcome for {node_id}")
                counts["tests"] += 1
                valid = set(phases) == {"setup", "call", "teardown"}
                for phase_name, phase in phases.items():
                    if not isinstance(phase, dict) or phase.get("outcome") not in {"passed", "failed", "skipped"}:
                        raise ValueError(f"Malformed phase for {node_id}")
                    if phase.get("wasxfail") is not None or phase["outcome"] == "skipped":
                        counts["skipped"] += 1
                        valid = False
                    elif phase["outcome"] == "failed":
                        counts["failures" if phase_name == "call" else "errors"] += 1
                        valid = False
                if not valid and not any(p.get("outcome") != "passed" for p in phases.values()):
                    counts["errors"] += 1
                outcomes.append(valid)
        passed = exit_code == 0 and all(outcomes) and not any(counts[k] for k in ("failures", "errors", "skipped"))
        if passed and any(suite.get("exit_code") != 0 for suite in suites.values()):
            raise ValueError("Suite exit code contradicts passing evidence")
        required_artifacts = {"protected.junit.xml", "generated.junit.xml"}
        if passed:
            required_artifacts |= {"api.json", "schema.json"}
        if not required_artifacts.issubset(artifacts):
            raise ValueError("Required validation artifact is missing")
        allowed_artifacts = required_artifacts | {"api.json", "schema.json", "protected.log", "generated.log"}
        for filename, content in artifacts.items():
            if filename not in allowed_artifacts or not isinstance(content, str):
                raise ValueError("Unexpected or malformed evidence artifact")
        for name, suite in suites.items():
            try:
                junit = ET.fromstring(artifacts[f"{name}.junit.xml"])
            except ET.ParseError as exc:
                raise ValueError(f"Malformed {name} JUnit artifact") from exc
            if passed:
                if len(junit.findall(".//testcase")) != len(suite["collected"]):
                    raise ValueError(f"{name} JUnit test count differs from JSON evidence")
                if any(junit.findall(f".//{outcome}") for outcome in ("failure", "error", "skipped")):
                    raise ValueError(f"{name} JUnit outcomes contradict passing evidence")
        if passed:
            api = json.loads(artifacts["api.json"])
            schema = json.loads(artifacts["schema.json"])
            if not isinstance(api, dict) or not api.get("openapi") or not api.get("paths"):
                raise ValueError("Missing OpenAPI schema")
            if not isinstance(schema, dict) or not any(table.get("name") == "links" for table in schema.get("tables", [])):
                raise ValueError("Missing links database schema")
        return {**counts, "passed": passed, "summary": evidence.get("summary", "Validation completed"),
                "protected_test_ids": protected["collected"], "evidence": evidence}, artifacts

    def validate(self, candidate: Path, output_dir: Path, scenario: str, alias_min: int = 4,
                 alias_max: int = 32, stop_requested: Callable[[], bool] | None = None) -> dict:
        if scenario not in SCENARIOS:
            raise ValueError(f"Unknown scenario: {scenario}")
        if not isinstance(alias_min, int) or not isinstance(alias_max, int) or not 1 <= alias_min <= alias_max <= 32:
            raise ValueError("Alias bounds must satisfy 1 <= minimum <= maximum <= 32")
        candidate = _checked_directory(candidate)
        for directory in (candidate / "service", candidate / "generated_tests"):
            _checked_directory(directory)
        if not (candidate / "service" / "shortener" / "app.py").is_file():
            raise ValueError("Candidate is missing service/shortener/app.py")
        for path in candidate.rglob("*"):
            if path.name in {"conftest.py", "pytest.ini", ".pytest.ini", "sitecustomize.py", "usercustomize.py"}:
                raise ValueError(f"Candidate test configuration is forbidden: {path.name}")
        expected = self._expected(scenario)
        fingerprint = _trusted_fingerprint(self.acceptance)
        _checked_directory(self.harness)
        environment = self.preflight()
        output_dir = Path(output_dir).absolute()
        if output_dir.exists() or output_dir.is_symlink():
            raise ValueError("Validation output directory must be a new path")
        if output_dir.parent.resolve() != output_dir.parent:
            raise ValueError("Validation output parent must not contain symlinks")
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(mode=0o700)
        token = uuid.uuid4().hex
        name = f"agentic-shortener-validation-{token}"
        report = {"mode": self.mode, "passed": False, "exit_code": -1, "tests": 0, "failures": 0, "errors": 1, "skipped": 0,
                  "summary": "Validation did not finish", "report_path": str(output_dir / "report.json"),
                  "container": name, "container_retained": True, "image_id": environment["image_id"],
                  "trusted_fingerprint": fingerprint}
        started, envelope = time.monotonic(), None
        try:
            if stop_requested and stop_requested():
                report.update(summary="Validation stopped before container creation", stopped=True, container_retained=False)
            else:
                command = self._create_command(name, token, environment["image_id"], candidate, scenario, alias_min, alias_max)
                created = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
                if created.returncode:
                    report.update(summary=f"Docker container creation failed: {created.stderr[:1000]}", container_retained=False)
                else:
                    capture = self._capture(name, token, stop_requested)
                    (output_dir / "stdout.log").write_bytes(capture["stdout"])
                    (output_dir / "stderr.log").write_bytes(capture["stderr"])
                    state = self._command("inspect", "--format", "{{json .State}}", name)
                    if state.returncode:
                        raise ValueError("Could not verify final Docker container state")
                    final_state = json.loads(state.stdout)
                    stopped_before_start = final_state.get("Status") == "created" and capture["termination"] == "stopped"
                    if final_state.get("Running") or (final_state.get("Status") != "exited" and not stopped_before_start):
                        self._kill_owned(name, token)
                        raise ValueError("Docker container has not exited")
                    exit_code = final_state["ExitCode"]
                    report.update(exit_code=exit_code, cli_exit_code=capture["cli_exit_code"],
                                  container_state=final_state, termination=capture["termination"])
                    if capture["termination"]:
                        report.update(summary=f"Validation ended: {capture['termination']}",
                                      stopped=capture["termination"] == "stopped")
                    elif capture["cli_exit_code"] != exit_code:
                        raise ValueError("Docker attach exit status differs from container exit status")
                    else:
                        envelope = json.loads(capture["stdout"])
                        result, artifacts = self._assess(envelope, expected, fingerprint, exit_code)
                        report.update(result)
                        report["artifacts"] = {}
                        for filename, content in artifacts.items():
                            destination = output_dir / filename
                            destination.write_text(content)
                            report["artifacts"][filename] = {"path": str(destination), "sha256": hashlib.sha256(content.encode()).hexdigest()}
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
            report.update(passed=False, summary=f"Validation evidence rejected: {exc}",
                          diagnostics=_rejected_diagnostics(envelope))
            try:
                self._kill_owned(name, token)
            except (OSError, subprocess.SubprocessError):
                pass
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        (output_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return report
