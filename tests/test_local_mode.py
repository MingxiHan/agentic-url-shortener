"""Local integration uses real domain rules and an explicit nonexecuting runner."""

import asyncio

import pytest

from orchestrator import cli
from orchestrator.artifacts import snapshot_files
from orchestrator.engine import Engine
from orchestrator.local_provider import LocalProvider
from orchestrator.runner import DockerRunner
from orchestrator.store import StateConflict, Store
from test_engine_guards import EvidenceRunnerDouble


def accept(engine, run):
    engine.approve(run["id"], "accept", run["data"]["pending"]["hash"], "unit-reviewer")
    return engine.store.get(run["id"])


def test_cli_uses_only_local_rules(tmp_path):
    engine = cli.make_engine(Store(tmp_path), "local")
    assert engine.provider.mode == engine.runner.mode == "local"
    assert isinstance(engine.provider, LocalProvider)
    assert isinstance(engine.runner, DockerRunner)
    assert engine.provider.preflight()["model_calls"] == 0
    for arguments in (["start", "greenfield"], ["doctor"],
                      ["start", "greenfield", "--mode", "local"], ["doctor", "--mode", "local"]):
        assert cli.parser().parse_args(arguments).mode == "local"
    for mode in ("fixture", "live", "unknown"):
        with pytest.raises(ValueError):
            cli.make_engine(engine.store, mode)
        with pytest.raises(SystemExit):
            cli.parser().parse_args(["start", "greenfield", "--mode", mode])


def test_engine_rejects_nonlocal_or_mismatched_execution_boundaries(tmp_path):
    provider, runner = LocalProvider(), EvidenceRunnerDouble()
    provider.mode = runner.mode = "unsupported"
    with pytest.raises(ValueError, match="Only local"):
        Engine(Store(tmp_path), provider, runner)
    provider.mode = "local"
    with pytest.raises(ValueError, match="modes must match"):
        Engine(Store(tmp_path), provider, runner)


def test_domain_value_error_stops_without_retry_or_replacement(tmp_path):
    class UnsupportedProvider(LocalProvider):
        async def respond(self, role, context, schema):
            raise ValueError("Unsupported domain request")

    store = Store(tmp_path)
    runner = EvidenceRunnerDouble()
    engine = Engine(store, UnsupportedProvider(), runner)
    run = engine.create("greenfield")
    result = asyncio.run(engine.run(run["id"]))
    assert result["status"] == "failed" and result["data"]["calls"] == 1
    assert store.read_artifact(result["data"]["handoff"]["id"])["permitted_action"] == "manual_review"
    assert not any(event["kind"] == "retry" for event in store.events(run["id"]))


def test_real_local_provider_alias_bounds_survive_plan_resume_and_text_revision(tmp_path):
    from pathlib import Path
    from orchestrator.domain import inspect_source

    store = Store(tmp_path)
    runner = EvidenceRunnerDouble()
    engine = Engine(store, LocalProvider(), runner)
    baseline = engine.create("greenfield")
    baseline = asyncio.run(engine.run(baseline["id"]))
    assert baseline["status"] == "waiting_for_approval", baseline["data"].get("error")
    baseline = accept(engine, baseline)
    run = engine.create("ambiguous", baseline=baseline["id"])
    run = asyncio.run(engine.run(run["id"]))
    assert run["status"] == "waiting_for_input"
    prior_approval = None
    for minimum, maximum, answer in (
        (8, 8, "Use optional custom aliases, exactly 8 characters; duplicate aliases return 409."),
        (4, 32, "Change custom aliases to 4-32 characters; duplicate aliases return 409."),
    ):
        store.revise(run["id"], answer)
        assert "normalization_contract" not in store.get(run["id"])["data"]
        revision = store.get(run["id"])["revision"]
        run = asyncio.run(engine.run(run["id"]))
        assert run["status"] == "waiting_for_approval", run["data"].get("error")
        assert run["data"]["pending"]["action"] == "plan"
        assert (run["data"]["alias_min"], run["data"]["alias_max"]) == (minimum, maximum)
        record = store.nodes(run["id"])["normalize"]["result"]
        normalized = store.read_artifact(record["id"])
        assert normalized["metadata"]["provider"] == "local_rules"
        assert normalized["metadata"]["model_calls"] == 0
        assert normalized["output"]["alias_min"] == minimum
        engine.approve(run["id"], "plan", run["data"]["pending"]["hash"], "unit-reviewer")
        run = asyncio.run(engine.run(run["id"]))
        assert run["status"] == "waiting_for_approval", run["data"].get("error")
        assert run["data"]["pending"]["action"] == "accept" and run["revision"] == revision
        assert store.nodes(run["id"])["normalize"]["attempts"] == 1
        if prior_approval:
            with pytest.raises(StateConflict):
                engine.approve(run["id"], "accept", prior_approval, "unit-reviewer")
        prior_approval = run["data"]["pending"]["hash"]
        release = store.read_artifact(run["data"]["release"]["id"])
        assert release["mode"] == "local" and "no model inference" in release["notice"]
        inventory = inspect_source(snapshot_files(Path(run["data"]["candidate"]["path"])))
        assert inventory["aliases"] and (inventory["alias_min"], inventory["alias_max"]) == (minimum, maximum)
    assert store.selected("local")["run_id"] == baseline["id"]
