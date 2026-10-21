"""Runner boundary tests use controlled subprocess fakes, never candidate host execution."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

import orchestrator.runner as runner_module
from orchestrator.runner import DockerRunner, PROTOCOL, RunnerUnavailable, _trusted_fingerprint


IMAGE_ID = "sha256:" + "a" * 64
EXPECTED = ["test_core.py::test_contract"]


def envelope(fingerprint="trusted"):
    phases = {name: {"outcome": "passed", "wasxfail": None, "duration": 0.01}
              for name in ("setup", "call", "teardown")}
    return {
        "protocol": PROTOCOL,
        "evidence": {
            "trusted_fingerprint": fingerprint,
            "summary": "Both suites passed",
            "suites": {
                "protected": {"collected": EXPECTED.copy(), "tests": {EXPECTED[0]: copy.deepcopy(phases)},
                              "collection_errors": [], "exit_code": 0},
                "generated": {"collected": ["test_generated.py::test_more"],
                              "tests": {"test_generated.py::test_more": copy.deepcopy(phases)},
                              "collection_errors": [], "exit_code": 0},
            },
        },
        "artifacts": {
            "protected.junit.xml": '<testsuites><testsuite tests="1"><testcase name="test_contract" /></testsuite></testsuites>',
            "generated.junit.xml": '<testsuites><testsuite tests="1"><testcase name="test_more" /></testsuite></testsuites>',
            "protected.log": "1 passed\n",
            "generated.log": "1 passed\n",
            "api.json": json.dumps({"openapi": "3.1.0", "paths": {"/health": {}}}),
            "schema.json": json.dumps({"tables": [{"name": "links", "sql": "CREATE TABLE links(code TEXT)"}]}),
        },
    }


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / "candidate with spaces"
    (root / "service" / "shortener").mkdir(parents=True)
    (root / "service" / "shortener" / "app.py").write_text("# Candidate code is never run by these tests.\n")
    (root / "generated_tests").mkdir()
    (root / "generated_tests" / "test_generated.py").write_text("def test_more():\n    assert True\n")
    return root


@pytest.fixture
def configured_runner(tmp_path, monkeypatch):
    runner = DockerRunner()
    runner.docker = "/usr/bin/docker"
    runner.acceptance = tmp_path / "acceptance"
    runner.acceptance.mkdir()
    (runner.acceptance / "test_core.py").write_text("def test_contract():\n    assert True\n")
    (runner.acceptance / "manifest.json").write_text(json.dumps({"greenfield": EXPECTED}))
    monkeypatch.setattr(runner, "preflight", lambda: {"available": True, "image_id": IMAGE_ID})
    return runner


def test_preflight_fails_closed_when_docker_is_missing(monkeypatch):
    monkeypatch.setattr(runner_module.shutil, "which", lambda name: None)
    with pytest.raises(RunnerUnavailable, match="Host execution is disabled"):
        DockerRunner().preflight()


def test_local_mode_is_the_only_docker_execution_mode(candidate):
    local = DockerRunner(mode="local")
    default = DockerRunner()
    assert local.mode == default.mode == "local"
    assert local._create_command("owned", "token", IMAGE_ID, candidate, "greenfield", 4, 32) == default._create_command(
        "owned", "token", IMAGE_ID, candidate, "greenfield", 4, 32)
    for mode in ("fixture", "live", "unknown"):
        with pytest.raises(ValueError, match="must be local"):
            DockerRunner(mode=mode)


@pytest.mark.parametrize("outputs, reason", [
    ([(1, "", "Cannot connect")], "daemon"),
    ([(0, "28.0.0", ""), (1, "", "No such image")], "Prepared runner image"),
    ([(0, "28.0.0", ""), (0, "tag-instead-of-digest", "")], "Prepared runner image"),
])
def test_preflight_checks_engine_and_immutable_image(monkeypatch, outputs, reason):
    monkeypatch.setattr(runner_module.shutil, "which", lambda name: "/usr/bin/docker")
    responses = iter(outputs)
    monkeypatch.setattr(DockerRunner, "_command", lambda self, *args: subprocess.CompletedProcess(args, *next(responses)))
    with pytest.raises(RunnerUnavailable, match=reason):
        DockerRunner().preflight()


def test_preflight_pins_prepared_image_id(monkeypatch):
    monkeypatch.setattr(runner_module.shutil, "which", lambda name: "/usr/bin/docker")
    outputs = iter([(0, "28.0.0\n", ""), (0, IMAGE_ID + "\n", "")])
    monkeypatch.setattr(DockerRunner, "_command", lambda self, *args: subprocess.CompletedProcess(args, *next(outputs)))
    assert DockerRunner().preflight()["image_id"] == IMAGE_ID


def test_container_has_no_host_writable_mount_or_credentials(configured_runner, candidate):
    command = configured_runner._create_command("owned", "token", IMAGE_ID, candidate, "greenfield", 4, 32)
    assert command[:2] == ["/usr/bin/docker", "create"]
    for flag, value in {"--network": "none", "--cap-drop": "ALL", "--security-opt": "no-new-privileges",
                        "--cpus": "1", "--memory": "512m", "--pids-limit": "64"}.items():
        assert command[command.index(flag) + 1] == value
    assert "--read-only" in command
    assert "--rm" not in command
    mounts = [command[index + 1] for index, flag in enumerate(command) if flag == "--mount"]
    assert len(mounts) == 4 and all(mount.endswith(",readonly") for mount in mounts)
    assert not any("docker.sock" in mount or "target=/home" in mount for mount in mounts)
    assert command[command.index("--user") + 1].split(":")[0] != "0"
    assert command[-8:] == [IMAGE_ID, "python", "-I", "-B", "/runner/harness.py", "greenfield", "4", "32"]


def test_passing_report_requires_full_protected_evidence_and_artifacts():
    report, artifacts = DockerRunner._assess(envelope(), EXPECTED, "trusted", 0)
    assert report["passed"] and report["tests"] == 2
    assert report["failures"] == report["errors"] == report["skipped"] == 0
    assert "api.json" in artifacts and "schema.json" in artifacts


@pytest.mark.parametrize("mutation, reason", [
    (lambda value: value["evidence"]["suites"]["protected"]["collected"].clear(), "Mandatory"),
    (lambda value: value["evidence"]["suites"]["protected"]["collected"].append(EXPECTED[0]), "Mandatory"),
    (lambda value: value["evidence"].update(trusted_fingerprint="tampered"), "fingerprint"),
    (lambda value: value["artifacts"].update({"../escaped.txt": "bad"}), "Unexpected"),
    (lambda value: value["artifacts"].pop("api.json"), "Required validation"),
    (lambda value: value["artifacts"].update({"protected.junit.xml": "invalid"}), "Malformed protected JUnit"),
    (lambda value: value["artifacts"].update({"protected.junit.xml": "<testsuites/>"}), "JUnit test count"),
    (lambda value: value["artifacts"].update({"schema.json": '{"tables":[]}'}), "links database"),
])
def test_missing_or_tampered_evidence_is_rejected(mutation, reason):
    value = envelope()
    mutation(value)
    with pytest.raises(ValueError, match=reason):
        DockerRunner._assess(value, EXPECTED, "trusted", 0)


@pytest.mark.parametrize("phase", [
    {"outcome": "skipped", "wasxfail": None},
    {"outcome": "skipped", "wasxfail": "expected failure"},
    {"outcome": "passed", "wasxfail": "unexpected pass"},
    {"outcome": "failed", "wasxfail": None, "longrepr": "assert 302 == 301"},
])
def test_skips_xfail_xpass_and_failure_cannot_pass(phase):
    value = envelope()
    value["evidence"]["suites"]["protected"]["tests"][EXPECTED[0]]["call"] = phase
    report, _ = DockerRunner._assess(value, EXPECTED, "trusted", 0)
    assert not report["passed"]
    assert report["evidence"]["suites"]["protected"]["tests"][EXPECTED[0]]["call"] == phase


def test_setup_or_teardown_omission_is_not_success():
    value = envelope()
    value["evidence"]["suites"]["protected"]["tests"][EXPECTED[0]].pop("teardown")
    report, _ = DockerRunner._assess(value, EXPECTED, "trusted", 0)
    assert not report["passed"] and report["errors"] == 1


def test_nonzero_container_status_cannot_claim_success():
    report, _ = DockerRunner._assess(envelope(), EXPECTED, "trusted", 137)
    assert not report["passed"]


def fake_container(monkeypatch, runner, payload, exit_code=0):
    monkeypatch.setattr(runner_module.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "container-id", ""))
    monkeypatch.setattr(runner, "_capture", lambda *args: {
        "stdout": json.dumps(payload).encode(), "stderr": b"", "termination": None, "cli_exit_code": exit_code,
    })
    monkeypatch.setattr(runner, "_command", lambda *args: subprocess.CompletedProcess(args, 0, json.dumps({
        "Status": "exited", "Running": False, "ExitCode": exit_code,
    }), ""))


def test_validation_preserves_actual_evidence_and_hashes(configured_runner, candidate, tmp_path, monkeypatch):
    payload = envelope(_trusted_fingerprint(configured_runner.acceptance))
    fake_container(monkeypatch, configured_runner, payload)
    output = tmp_path / "evidence"
    result = configured_runner.validate(candidate, output, "greenfield")
    assert result["passed"] and result["container_retained"]
    assert json.loads((output / "stdout.log").read_text()) == payload
    assert json.loads(Path(result["report_path"]).read_text()) == result
    assert result["artifacts"]["api.json"]["sha256"] == hashlib.sha256((output / "api.json").read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="new path"):
        configured_runner.validate(candidate, output, "greenfield")


def test_local_validation_report_retains_its_mode(configured_runner, candidate, tmp_path, monkeypatch):
    configured_runner.mode = "local"
    fake_container(monkeypatch, configured_runner, envelope(_trusted_fingerprint(configured_runner.acceptance)))
    report = configured_runner.validate(candidate, tmp_path / "local-evidence", "greenfield")
    assert report["passed"] and report["mode"] == "local"
    assert json.loads(Path(report["report_path"]).read_text())["mode"] == "local"


def test_invalid_evidence_remains_a_failed_retained_report(configured_runner, candidate, tmp_path, monkeypatch):
    fake_container(monkeypatch, configured_runner, envelope("modified"))
    result = configured_runner.validate(candidate, tmp_path / "evidence", "greenfield")
    assert not result["passed"] and "fingerprint" in result["summary"]
    assert Path(result["report_path"]).exists()


def test_collection_failure_retains_untrusted_repair_diagnostics_without_passing(
    configured_runner, candidate, tmp_path, monkeypatch,
):
    runner = configured_runner
    payload = envelope(_trusted_fingerprint(runner.acceptance))
    error = "ImportError while importing test_core.py\nservice/shortener/app.py:19: SyntaxError: invalid syntax"
    payload["evidence"]["suites"]["protected"].update(
        collected=[], tests={}, collection_errors=[error], exit_code=2,
    )
    payload["artifacts"]["protected.log"] = error + "\nInterrupted: 1 error during collection\n"
    fake_container(monkeypatch, runner, payload, exit_code=2)
    output = tmp_path / "failed-evidence"
    result = runner.validate(candidate, output, "greenfield")
    assert not result["passed"] and result["errors"] == 1 and result["exit_code"] == 2
    assert "Mandatory protected test IDs" in result["summary"]
    assert result["diagnostics"]["trusted_for_acceptance"] is False
    assert result["diagnostics"]["collection_errors"]["protected"] == [error]
    assert result["diagnostics"]["logs"]["protected.log"] == payload["artifacts"]["protected.log"]
    assert json.loads((output / "stdout.log").read_text()) == payload
    assert json.loads(Path(result["report_path"]).read_text())["diagnostics"] == result["diagnostics"]


def test_rejected_diagnostics_are_bounded_and_tolerate_malformed_fields():
    payload = envelope()
    payload["evidence"]["suites"]["protected"]["collection_errors"] = ["\U0001f600" * 20000] * 10
    payload["artifacts"]["protected.log"] = "x" * 100000
    result = runner_module._rejected_diagnostics(payload)
    assert result["truncated"] and not result["trusted_for_acceptance"]
    excerpts = result["collection_errors"]["protected"] + list(result["logs"].values())
    assert sum(len(text.encode()) for text in excerpts) <= runner_module.MAX_DIAGNOSTIC_BYTES
    malformed = runner_module._rejected_diagnostics({"evidence": [], "artifacts": []})
    assert malformed["logs"] == {} and not malformed["trusted_for_acceptance"]


def test_stop_before_dispatch_never_starts_docker(configured_runner, candidate, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Docker execution must not start after stop")
    monkeypatch.setattr(runner_module.subprocess, "run", forbidden)
    result = configured_runner.validate(candidate, tmp_path / "evidence", "greenfield", stop_requested=lambda: True)
    assert not result["passed"] and result["stopped"] and not result["container_retained"]


@pytest.mark.parametrize("filename", ["conftest.py", "pytest.ini", "sitecustomize.py"])
def test_candidate_cannot_supply_pytest_or_python_startup_configuration(configured_runner, candidate, tmp_path, filename):
    (candidate / "generated_tests" / filename).write_text("# forbidden\n")
    with pytest.raises(ValueError, match="configuration is forbidden"):
        configured_runner.validate(candidate, tmp_path / "evidence", "greenfield")


def test_symlink_candidate_is_rejected(configured_runner, candidate, tmp_path):
    (candidate / "generated_tests" / "leak.py").symlink_to(candidate / "service" / "shortener" / "app.py")
    with pytest.raises(ValueError, match="Unsupported candidate"):
        configured_runner.validate(candidate, tmp_path / "evidence", "greenfield")


def test_output_symlink_ancestor_is_rejected_before_creating_external_parent(
    configured_runner, candidate, tmp_path, monkeypatch,
):
    runner = configured_runner
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Unsafe output must not dispatch"))
    with pytest.raises(ValueError, match="symlinks"):
        runner.validate(candidate, link / "new-parent" / "evidence", "greenfield")
    assert not (outside / "new-parent").exists()


@pytest.mark.parametrize("cause", ["stopped", "timeout", "output_limit"])
def test_running_container_is_killed_on_stop_timeout_or_output_limit(monkeypatch, cause):
    # This is a developer-authored inert subprocess standing in for Docker's attach client.
    real_popen = subprocess.Popen
    body = "import time; time.sleep(5)"
    if cause == "output_limit":
        body = "import sys; sys.stdout.buffer.write(b'x' * (5 * 1024 * 1024)); sys.stdout.flush()"
    monkeypatch.setattr(runner_module.subprocess, "Popen", lambda *args, **kwargs: real_popen(
        [sys.executable, "-c", body], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ))
    runner = DockerRunner(timeout=0.01 if cause == "timeout" else 5)
    killed = []
    monkeypatch.setattr(runner, "_kill_owned", lambda name, token: killed.append((name, token)))
    started = time.monotonic()
    stop_calls = []
    def stop_requested():
        stop_calls.append(True)
        return cause == "stopped" and len(stop_calls) > 1
    result = runner._capture("owned-container", "token", stop_requested)
    assert result["termination"] == cause
    assert killed == [("owned-container", "token")]
    assert len(result["stdout"]) <= runner_module.MAX_OUTPUT_BYTES
    assert time.monotonic() - started < 3


def test_kill_requires_owned_container_label(monkeypatch):
    runner = DockerRunner()
    commands = []
    def command(*args):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "somebody-elses-token\n", "")
    monkeypatch.setattr(runner, "_command", command)
    runner._kill_owned("container", "my-token")
    assert len(commands) == 1 and commands[0][0] == "inspect"
