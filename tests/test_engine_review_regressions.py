"""Review regressions use real rules and explicit nonexecuting runner doubles."""

import asyncio

import pytest

from orchestrator import cli
from orchestrator.engine import Engine
from orchestrator.local_provider import LocalProvider
from orchestrator.store import StateConflict, Store
from test_engine_guards import EvidenceRunnerDouble


class RecordingRunner(EvidenceRunnerDouble):
    mode = "local"

    def __init__(self):
        super().__init__()
        self.contracts = []

    def validate(self, candidate, output_dir, scenario, alias_min, alias_max, stopped):
        self.contracts.append((scenario, alias_min, alias_max))
        return super().validate(candidate, output_dir, scenario, alias_min, alias_max, stopped)


def accepted(engine, scenario="greenfield", requirement=None, baseline=None):
    run = engine.create(scenario, requirement=requirement, baseline=baseline)
    run = asyncio.run(engine.run(run["id"]))
    if run["status"] == "waiting_for_approval" and run["data"]["pending"]["action"] == "plan":
        engine.approve(run["id"], "plan", run["data"]["pending"]["hash"], "unit-reviewer")
        run = asyncio.run(engine.run(run["id"]))
    assert run["status"] == "waiting_for_approval", run["data"].get("error")
    assert run["data"]["pending"]["action"] == "accept"
    engine.approve(run["id"], "accept", run["data"]["pending"]["hash"], "unit-reviewer")
    return engine.store.get(run["id"])


def alias_request(bounds):
    return f"Use optional custom aliases, {bounds[0]}-{bounds[1]} characters; duplicate aliases return 409."


@pytest.mark.parametrize("explicit", [True, False])
def test_local_alias_request_requires_only_missing_clarification(tmp_path, explicit):
    engine = Engine(Store(tmp_path), LocalProvider(), RecordingRunner())
    core = accepted(engine)
    requirement = alias_request((8, 8)) if explicit else "Make links easier to share."
    run = engine.create("ambiguous", requirement=requirement, baseline=core["id"])
    run = asyncio.run(engine.run(run["id"]))
    assert run["data"]["answers"] == "" and run["revision"] == 1
    if explicit:
        assert run["status"] == "waiting_for_approval"
        assert run["data"]["pending"]["action"] == "plan"
        assert (run["data"]["alias_min"], run["data"]["alias_max"]) == (8, 8)
        engine.approve(run["id"], "plan", run["data"]["pending"]["hash"], "unit-reviewer")
        completed = asyncio.run(engine.run(run["id"]))
        assert completed["data"]["pending"]["action"] == "accept"
        assert engine.runner.contracts[-1] == ("ambiguous", 8, 8)
        assert engine.store.nodes(run["id"])["normalize"]["attempts"] == 1
    else:
        assert run["status"] == "waiting_for_input" and run["data"]["questions"]
        assert "plan" not in engine.store.nodes(run["id"])


@pytest.mark.parametrize("failed_write", ["_handoff", "_write_summary"])
def test_failure_record_remains_terminal_when_final_artifact_write_fails(tmp_path, monkeypatch, failed_write):
    class FailOnceProvider(LocalProvider):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def respond(self, role, context, schema):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Primary workflow failure retained in state")
            return await super().respond(role, context, schema)

    store, provider = Store(tmp_path), FailOnceProvider()
    engine = Engine(store, provider, EvidenceRunnerDouble())

    def write_failure(*args):
        raise OSError("Simulated final artifact write failure")

    monkeypatch.setattr(engine, failed_write, write_failure)
    run = engine.create("greenfield")
    with pytest.raises(OSError, match="final artifact write failure"):
        asyncio.run(engine.run(run["id"]))
    retained = store.get(run["id"])
    assert retained["status"] == "failed"
    assert retained["data"]["error"] == "Primary workflow failure retained in state"
    assert bool(retained["data"].get("handoff")) is (failed_write == "_write_summary")
    assert provider.calls == retained["data"]["calls"] == 1
    assert any(event["kind"] == "safe_stop" for event in store.events(run["id"]))

    monkeypatch.setattr(cli, "make_engine", lambda current, mode: Engine(current, provider, EvidenceRunnerDouble()))
    for command in (["resume", run["id"]],
                    ["answer", run["id"], "--text", "Preserve the existing contract"],
                    ["stop", run["id"]]):
        assert cli.main(["--runs-dir", str(tmp_path), *command]) == 2
        assert store.get(run["id"]) == retained
        assert provider.calls == 1
    with pytest.raises(StateConflict):
        store.node(run["id"], run["revision"], "late", "passed")
    assert store.get(run["id"]) == retained


@pytest.mark.parametrize("scenario", ["greenfield", "brownfield"])
def test_local_nonalias_work_inherits_accepted_alias_contract(tmp_path, scenario):
    runner = RecordingRunner()
    engine = Engine(Store(tmp_path), LocalProvider(), runner)
    core = accepted(engine)
    aliases = accepted(engine, "ambiguous", alias_request((8, 8)), core["id"])
    changed = accepted(engine, scenario, baseline=aliases["id"])
    assert (changed["data"]["alias_min"], changed["data"]["alias_max"]) == (8, 8)
    assert runner.contracts[-1] == (scenario, 8, 8)
    assert changed["data"]["baseline"] == aliases["data"]["release"]


@pytest.mark.parametrize("initial,current", [((8, 8), (4, 32)), ((4, 32), (8, 8))])
def test_recovery_uses_current_baseline_alias_contract_not_failed_parent(tmp_path, initial, current):
    store, runner = Store(tmp_path), RecordingRunner()
    engine = Engine(store, LocalProvider(), runner)
    core = accepted(engine)
    aliases = accepted(engine, "ambiguous", alias_request(initial), core["id"])
    limited = Engine(store, LocalProvider(), RecordingRunner(), max_calls=0)
    parent = limited.create("brownfield", baseline=aliases["id"])
    parent = asyncio.run(limited.run(parent["id"]))
    assert parent["status"] == "failed"
    assert (parent["data"]["alias_min"], parent["data"]["alias_max"]) == initial
    selected = accepted(engine, "ambiguous", alias_request(current), aliases["id"])
    child = engine.recover(parent["id"], parent["data"]["handoff"]["hash"], "unit-reviewer", "Preserve the current accepted contract")
    assert (child["data"]["alias_min"], child["data"]["alias_max"]) == current
    assert child["data"]["baseline"] == selected["data"]["release"]
    pending = asyncio.run(engine.run(child["id"]))
    assert pending["data"]["pending"]["action"] == "plan"
    engine.approve(child["id"], "plan", pending["data"]["pending"]["hash"], "unit-reviewer")
    result = asyncio.run(engine.run(child["id"]))
    assert result["data"]["pending"]["action"] == "accept"
    assert runner.contracts[-1] == ("brownfield", *current)
    assert store.get(parent["id"])["data"]["alias_min"] == initial[0]
