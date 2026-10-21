"""Portable evidence integrity tests use explicit runner doubles, never candidate execution."""

import asyncio
import hashlib
import json
from pathlib import Path
import shutil

import pytest

import orchestrator.bundle as bundle
from orchestrator.engine import Engine
from orchestrator.contracts import SCENARIOS
from orchestrator.local_provider import LocalProvider
from orchestrator.runner import DockerRunner, PROJECT_ROOT, PROTOCOL, _trusted_fingerprint
from orchestrator.store import Store


class BundleRunnerDouble:
    """Synthetic validation envelopes exercise export bindings, not service correctness."""

    mode = "local"

    def preflight(self):
        return {"notice": "Unit-test double; no Docker or candidate execution"}

    def validate(self, candidate, output_dir, scenario, alias_min, alias_max, stopped):
        expected = json.loads((PROJECT_ROOT / "acceptance/manifest.json").read_text())[scenario]
        fingerprint = _trusted_fingerprint(PROJECT_ROOT / "acceptance")
        phases = {name: {"outcome": "passed", "wasxfail": None, "duration": 0.01}
                  for name in ("setup", "call", "teardown")}
        suites = {}
        artifacts = {}
        for name, ids in (("protected", expected), ("generated", ["test_synthetic.py::test_contract"])):
            suites[name] = {"collected": ids, "tests": {node: phases for node in ids},
                            "collection_errors": [], "exit_code": 0}
            cases = "".join(f'<testcase name="synthetic_{index}" />' for index in range(len(ids)))
            artifacts[f"{name}.junit.xml"] = f"<testsuites><testsuite>{cases}</testsuite></testsuites>"
            artifacts[f"{name}.log"] = "Synthetic unit-test envelope; no service execution.\n"
        artifacts.update({
            "api.json": json.dumps({"openapi": "3.1.0", "paths": {"/health": {}}}),
            "schema.json": json.dumps({"tables": [{"name": "links", "sql": "CREATE TABLE links(code TEXT)"}]}),
        })
        envelope = {"protocol": PROTOCOL, "evidence": {
            "trusted_fingerprint": fingerprint, "summary": "Synthetic bundle-test evidence", "suites": suites,
        }, "artifacts": artifacts}
        result, artifacts = DockerRunner._assess(envelope, expected, fingerprint, 0)
        result.update(mode="local", exit_code=0, cli_exit_code=0, image_id="sha256:" + "a" * 64,
                      container_state={"Status": "exited", "Running": False, "ExitCode": 0}, artifacts={})
        output_dir.mkdir(parents=True)
        (output_dir / "stdout.log").write_text(json.dumps(envelope))
        (output_dir / "stderr.log").write_text("")
        for name, content in artifacts.items():
            target = output_dir / name
            target.write_text(content)
            result["artifacts"][name] = {"path": str(target), "sha256": hashlib.sha256(content.encode()).hexdigest()}
        (output_dir / "report.json").write_text(json.dumps(result))
        return result


@pytest.fixture(scope="module")
def accepted_runs(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("bundle-source").resolve())
    engine = Engine(store, LocalProvider(), BundleRunnerDouble())

    async def run_to_accept(run_id):
        run = await engine.run(run_id)
        if run["status"] == "waiting_for_approval" and run["data"]["pending"]["action"] == "plan":
            engine.approve(run_id, "plan", run["data"]["pending"]["hash"], "bundle-test-reviewer")
            run = await engine.run(run_id)
        assert run["status"] == "waiting_for_approval", run["data"].get("error")
        assert run["data"]["pending"]["action"] == "accept"
        return run

    first = engine.create("greenfield")
    asyncio.run(run_to_accept(first["id"]))
    store.revise(first["id"], SCENARIOS["greenfield"])
    first = asyncio.run(run_to_accept(first["id"]))
    engine.approve(first["id"], "accept", first["data"]["pending"]["hash"], "bundle-test-reviewer")
    second = engine.create("brownfield", baseline=first["id"])
    second = asyncio.run(run_to_accept(second["id"]))
    engine.approve(second["id"], "accept", second["data"]["pending"]["hash"], "bundle-test-reviewer")
    return store, first["id"], second["id"]


@pytest.fixture
def exported(accepted_runs, tmp_path):
    store, first, second = accepted_runs
    destination = tmp_path.resolve() / "bundle"
    result = bundle.export_bundle(store, [second], destination)
    assert result["verified"]
    return destination, result


def document(path):
    return json.loads((path / "manifest.json").read_text())


def rewrite_manifest(path, value):
    (path / "manifest.json").write_text(json.dumps(value))


def rewrite_manifested_json(path, relative, value):
    target = path / relative
    raw = json.dumps(value, sort_keys=True).encode()
    target.write_bytes(raw)
    manifest = document(path)
    manifest["files"][relative] = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
    rewrite_manifest(path, manifest)


def test_export_preserves_raw_provenance_and_recursive_baseline(accepted_runs, exported):
    store, first, second = accepted_runs
    path, result = exported
    assert set(result["runs"]) == {first, second}
    assert result["requested_runs"] == [second]
    assert {item["run_id"] for item in result["accepted"]} == {first, second}
    assert all(item["mode"] == "local" and Path(item["candidate_path"]).is_relative_to(path) for item in result["accepted"])
    metadata = json.loads((path / "metadata.json").read_text())
    assert {node["revision"] for node in metadata["nodes"] if node["run_id"] == first} == {1, 2}
    assert any(node["status"] == "stale" for node in metadata["nodes"])
    manifest = document(path)
    for record in metadata["artifacts"]:
        relative = manifest["index"]["artifacts"][record["id"]]
        assert (path / relative).read_bytes() == Path(record["path"]).read_bytes()
    assert manifest["index"]["prompts"] == {}
    assert any(name.startswith("provenance/orchestrator/") for name in manifest["files"])
    assert not any("/runtime/" in name or name.endswith(".sqlite3") for name in manifest["files"])
    assert "does not rerun" in result["notice"] and "not signed" in result["notice"]


@pytest.fixture(scope="module")
def recovered_runs(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("bundle-recovery").resolve())
    limited = Engine(store, LocalProvider(), BundleRunnerDouble(), max_calls=1)
    parent = limited.create("greenfield")
    parent = asyncio.run(limited.run(parent["id"]))
    assert parent["status"] == "failed" and parent["data"]["handoff"]
    engine = Engine(store, LocalProvider(), BundleRunnerDouble())
    child = engine.recover(parent["id"], parent["data"]["handoff"]["hash"],
                           "synthetic-recovery-reviewer", "Use the standard bounded budget; preserve the core contract.")
    child = asyncio.run(engine.run(child["id"]))
    assert child["status"] == "waiting_for_approval", child["data"].get("error")
    engine.approve(child["id"], "accept", child["data"]["pending"]["hash"], "synthetic-recovery-reviewer")
    return store, parent["id"], child["id"]


@pytest.mark.parametrize("selection", [1, 2])
def test_recovery_export_retains_both_sides_of_lineage(recovered_runs, tmp_path, selection):
    store, parent, child = recovered_runs
    result = bundle.export_bundle(store, [recovered_runs[selection]], tmp_path.resolve() / "recovery")
    assert set(result["runs"]) == {parent, child}
    assert [entry["run_id"] for entry in result["accepted"]] == [child]


@pytest.mark.parametrize("change", ["approval", "instructions", "parent_budget", "parent_link"])
def test_recovery_binding_survives_rehashed_inventory(recovered_runs, tmp_path, change):
    store, parent, child = recovered_runs
    destination = tmp_path.resolve() / "recovery"
    bundle.export_bundle(store, [child], destination)
    metadata = json.loads((destination / "metadata.json").read_text())
    runs = {run["id"]: run for run in metadata["runs"]}
    if change == "approval":
        for approval in metadata["approvals"]:
            if approval["action"] == "recover":
                approval["valid"] = 0
    elif change == "instructions":
        runs[child]["data"]["recovery"]["instructions"] = "Different authorization"
    elif change == "parent_budget":
        runs[parent]["data"]["calls"] = 0
    else:
        runs[parent]["data"]["recovery_child"] = parent
    rewrite_manifested_json(destination, "metadata.json", metadata)
    with pytest.raises(bundle.BundleError, match="[Rr]ecovery|[Hh]andoff|budgets"):
        bundle.verify_bundle(destination)


def test_relocated_bundle_ignores_original_absolute_paths(exported, tmp_path, monkeypatch):
    path, _ = exported
    relocated = tmp_path.resolve() / "relocated"
    shutil.copytree(path, relocated)
    metadata = json.loads((relocated / "metadata.json").read_text())

    def replace_paths(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "path" and isinstance(item, str) and item.startswith("/"):
                    value[key] = "/unavailable-original-machine/archival-only"
                else:
                    replace_paths(item)
        elif isinstance(value, list):
            for item in value:
                replace_paths(item)
    replace_paths(metadata)
    rewrite_manifested_json(relocated, "metadata.json", metadata)
    monkeypatch.setattr(bundle, "_source", lambda *args: pytest.fail("Verification must not read original absolute paths"))
    result = bundle.verify_bundle(relocated)
    assert result["verified"] and all(Path(item["candidate_path"]).is_relative_to(relocated) for item in result["accepted"])


def test_byte_tampering_is_rejected(exported):
    path, result = exported
    code = Path(result["accepted"][0]["candidate_path"]) / "service/shortener/app.py"
    with code.open("a") as stream:
        stream.write("\n# Changed after export.\n")
    with pytest.raises(bundle.BundleError, match="hash or size"):
        bundle.verify_bundle(path)


def test_candidate_content_hash_survives_rewritten_inventory(exported):
    path, result = exported
    code = Path(result["accepted"][0]["candidate_path"]) / "service/shortener/app.py"
    raw = code.read_bytes() + b"\n# Changed candidate.\n"
    code.write_bytes(raw)
    manifest = document(path)
    manifest["files"][code.relative_to(path).as_posix()] = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
    rewrite_manifest(path, manifest)
    with pytest.raises(bundle.BundleError, match="Candidate contents"):
        bundle.verify_bundle(path)


@pytest.mark.parametrize("unsafe", ["../outside", "/absolute/path", "C:/windows", "safe/../../escape", "dir\\escape", "dir//file"])
def test_traversal_and_absolute_index_paths_are_rejected(exported, unsafe):
    path, _ = exported
    manifest = document(path)
    key = next(iter(manifest["index"]["artifacts"]))
    manifest["index"]["artifacts"][key] = unsafe
    rewrite_manifest(path, manifest)
    with pytest.raises(bundle.BundleError):
        bundle.verify_bundle(path)


def test_symlinks_and_unmanifested_files_are_rejected(exported):
    path, _ = exported
    (path / "unexpected-link").symlink_to("/unavailable-original-machine")
    with pytest.raises(bundle.BundleError, match="symlinks"):
        bundle.verify_bundle(path)


def test_missing_approval_is_rejected_even_with_rehashed_metadata(exported):
    path, _ = exported
    metadata = json.loads((path / "metadata.json").read_text())
    for approval in metadata["approvals"]:
        if approval["action"] == "accept":
            approval["valid"] = 0
    rewrite_manifested_json(path, "metadata.json", metadata)
    with pytest.raises(bundle.BundleError, match="valid human approval"):
        bundle.verify_bundle(path)


def test_nonlocal_evidence_is_rejected(exported):
    path, _ = exported
    metadata = json.loads((path / "metadata.json").read_text())
    for run in metadata["runs"]:
        run["mode"] = "live"
    rewrite_manifested_json(path, "metadata.json", metadata)
    with pytest.raises(bundle.BundleError, match="Only local execution evidence"):
        bundle.verify_bundle(path)


def test_validation_remains_bound_to_original_alias_contract(exported):
    path, _ = exported
    metadata = json.loads((path / "metadata.json").read_text())
    metadata["runs"][0]["data"].update(alias_min=8, alias_max=8)
    rewrite_manifested_json(path, "metadata.json", metadata)
    with pytest.raises(bundle.BundleError, match="input hash differs"):
        bundle.verify_bundle(path)


def test_evidence_mapping_cannot_omit_required_logs(exported):
    path, _ = exported
    manifest = document(path)
    entry = next(iter(manifest["index"]["evidence"].values()))
    entry["files"].pop("stdout.log")
    rewrite_manifest(path, manifest)
    with pytest.raises(bundle.BundleError, match="omitted a required"):
        bundle.verify_bundle(path)


def test_size_limits_fail_closed(exported, monkeypatch):
    path, _ = exported
    monkeypatch.setattr(bundle, "MAX_TOTAL_BYTES", 100)
    with pytest.raises(bundle.BundleError, match="hash or size"):
        bundle.verify_bundle(path)


def test_export_refuses_to_overwrite_existing_destination(accepted_runs, exported):
    store, _, second = accepted_runs
    path, _ = exported
    before = (path / "manifest.json").read_bytes()
    with pytest.raises(bundle.BundleError, match="new directory"):
        bundle.export_bundle(store, [second], path)
    assert (path / "manifest.json").read_bytes() == before


def test_export_rejects_symlink_ancestor_before_creating_external_directory(accepted_runs, tmp_path):
    store, _, second = accepted_runs
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    link = tmp_path.resolve() / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(bundle.BundleError, match="without following symlinks"):
        bundle.export_bundle(store, [second], link / "new-parent" / "bundle")
    assert not (outside / "new-parent").exists()


def smoke_store(tmp_path, report, logs=()):
    """Record synthetic smoke evidence without executing candidates or removing files."""
    store = Store(tmp_path.resolve() / "smoke-store")
    run = store.create("greenfield", "local", "Synthetic smoke export boundary test")
    directory = store.root / run["id"] / "rollback-smoke"
    directory.mkdir()
    path = directory / "report.json"
    if report is not None:
        path.write_text(json.dumps(report))
    for name in logs:
        (directory / name).write_text("Retained synthetic execution log.\n")
    store.event(run["id"], run["revision"], "rollback_smoke", {
        "passed": report["passed"] if report is not None else False,
        "target": run["id"], "report": str(path),
    })
    return store, run["id"]


@pytest.mark.parametrize("details", [
    {"mode": "local", "stopped": True, "container_retained": False,
     "summary": "Validation stopped before container creation"},
    {"mode": "local", "container_retained": False, "summary": "Docker container creation failed: test failure"},
])
def test_rollback_smoke_export_allows_absent_logs_only_before_execution(tmp_path, details):
    report = {"passed": False, "exit_code": -1, "tests": 0, **details}
    store, run_id = smoke_store(tmp_path, report)
    path = tmp_path.resolve() / "bundle"
    assert bundle.export_bundle(store, [run_id], path)["verified"]
    mapping = next(value for key, value in document(path)["index"]["evidence"].items() if key.startswith("event-"))
    assert set(mapping["files"]) == {"report.json"}
    assert json.loads((path / mapping["files"]["report.json"]).read_text()) == report


def test_rollback_smoke_export_retains_present_optional_logs(tmp_path):
    report = {"mode": "local", "passed": False, "exit_code": -1, "tests": 0,
              "stopped": True, "container_retained": False, "summary": "Validation stopped before container creation"}
    store, run_id = smoke_store(tmp_path, report, logs=("stdout.log",))
    path = tmp_path.resolve() / "bundle"
    assert bundle.export_bundle(store, [run_id], path)["verified"]
    mapping = next(iter(document(path)["index"]["evidence"].values()))
    assert set(mapping["files"]) == {"report.json", "stdout.log"}


@pytest.mark.parametrize("report,logs", [
    ({"passed": True, "exit_code": 0, "tests": 10}, ()),
    ({"passed": True, "exit_code": 0, "tests": 10}, ("stdout.log",)),
    ({"passed": True, "exit_code": 0, "tests": 10}, ("stderr.log",)),
    ({"passed": False, "exit_code": 1, "tests": 10}, ()),
    ({"passed": False, "exit_code": 137, "tests": 0, "stopped": True,
      "container_retained": True, "summary": "Validation ended: stopped"}, ()),
    ({"passed": True, "exit_code": -1, "tests": 0, "stopped": True,
      "container_retained": False, "summary": "Validation stopped before container creation"}, ()),
])
def test_rollback_smoke_export_rejects_missing_executed_or_passed_logs(tmp_path, report, logs):
    store, run_id = smoke_store(tmp_path, report, logs)
    destination = tmp_path.resolve() / "bundle"
    with pytest.raises(bundle.BundleError, match="Rollback smoke evidence is missing required file"):
        bundle.export_bundle(store, [run_id], destination)
    assert not destination.exists()


def test_rollback_smoke_missing_report_is_a_bundle_error(tmp_path):
    store, run_id = smoke_store(tmp_path, None)
    with pytest.raises(bundle.BundleError, match="Rollback smoke report is missing"):
        bundle.export_bundle(store, [run_id], tmp_path.resolve() / "bundle")


def test_rollback_smoke_verification_cannot_drop_passed_logs_from_mapping(tmp_path):
    store, run_id = smoke_store(tmp_path, {"passed": True, "exit_code": 0, "tests": 10},
                               logs=("stdout.log", "stderr.log"))
    path = tmp_path.resolve() / "bundle"
    bundle.export_bundle(store, [run_id], path)
    manifest = document(path)
    next(iter(manifest["index"]["evidence"].values()))["files"].pop("stdout.log")
    rewrite_manifest(path, manifest)
    with pytest.raises(bundle.BundleError, match="Rollback smoke evidence is missing required files"):
        bundle.verify_bundle(path)
