"""Explicit recovery is a new authorized run, never a reset or gate bypass."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier

import pytest

from orchestrator import cli
from orchestrator.artifacts import PolicyError
from orchestrator.engine import Engine, GateFailure, ProviderFailure, RecoverableFailure
from orchestrator.local_provider import LocalProvider
from orchestrator.store import StateConflict, Store
from test_engine_guards import EvidenceRunnerDouble, SyntheticRepairProvider


def engine(store, **limits):
    return Engine(store, LocalProvider(), EvidenceRunnerDouble(), **limits)


def failed_parent(store, **limits):
    bounded = engine(store, **(limits or {"max_calls": 0}))
    run = bounded.create("greenfield")
    return asyncio.run(bounded.run(run["id"]))


def recover(store, parent):
    return engine(store).recover(parent["id"], parent["data"]["handoff"]["hash"],
                                 "human-reviewer", "Review the prior evidence and rebuild within the existing contract.")


def accepted(store, baseline=None):
    worker = engine(store)
    run = worker.create("brownfield" if baseline else "greenfield", baseline=baseline)
    run = asyncio.run(worker.run(run["id"]))
    if run["data"]["pending"]["action"] == "plan":
        worker.approve(run["id"], "plan", run["data"]["pending"]["hash"], "human-reviewer")
        run = asyncio.run(worker.run(run["id"]))
    worker.approve(run["id"], "accept", run["data"]["pending"]["hash"], "human-reviewer")
    return store.get(run["id"])


def test_handoff_freezes_parent_and_retains_budget_and_evidence(tmp_path):
    store = Store(tmp_path)
    parent = failed_parent(store, max_calls=3)
    handoff = store.read_artifact(parent["data"]["handoff"]["id"])
    assert handoff["failure_code"] == "call_budget_exhausted"
    assert handoff["failed_node"]
    assert handoff["revision"] == 1 and handoff["baseline"] is None
    assert handoff["evidence"] and handoff["reason"]
    assert handoff["budgets"]["calls"] == parent["data"]["calls"] == 3
    assert handoff["budgets"]["active_seconds"] == parent["data"]["active_seconds"]
    assert handoff["permitted_action"] == "create_replacement"
    for action in (lambda: asyncio.run(engine(store).run(parent["id"])),
                   lambda: store.revise(parent["id"], "Reset the budget"),
                   lambda: store.stop(parent["id"]),
                   lambda: store.node(parent["id"], 1, "late", "passed")):
        with pytest.raises(StateConflict):
            action()
    assert store.get(parent["id"])["status"] == "failed"


def test_cli_recovery_creates_only_then_child_uses_normal_gates(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path)
    parent = failed_parent(store)
    monkeypatch.setattr(cli, "make_engine", lambda current, mode: engine(current))
    code = cli.main(["--runs-dir", str(tmp_path), "recover", parent["id"],
                     "--hash", parent["data"]["handoff"]["hash"], "--actor", "reviewer",
                     "--instructions", "Preserve the API and inspect the failed call budget."])
    assert code == 0
    child = store.get(json.loads(capsys.readouterr().out)["id"])
    assert child["data"]["calls"] == child["data"]["active_seconds"] == 0
    assert child["data"]["baseline"] is None and store.nodes(child["id"]) == {}
    assert child["data"]["recovery"]["actor"] == "reviewer"
    worker = engine(store)
    assert worker._context(child["id"])["recovery_instructions"].startswith("Preserve")
    child = asyncio.run(worker.run(child["id"]))
    assert child["status"] == "waiting_for_approval"
    assert child["data"]["pending"]["action"] == "accept"
    assert store.selected("local") is None
    worker.approve(child["id"], "accept", child["data"]["pending"]["hash"], "reviewer")
    assert store.get(child["id"])["status"] == "succeeded"
    retained = store.get(parent["id"])
    assert retained["status"] == "failed" and retained["data"]["calls"] == parent["data"]["calls"]
    assert retained["data"]["active_seconds"] == parent["data"]["active_seconds"]
    assert retained["data"]["recovery_child"] == child["id"]
    decisions = [event["payload"] for event in store.events(parent["id"]) if event["kind"] == "human_decision"]
    assert decisions == [{"action": "recover", "hash": parent["data"]["handoff"]["hash"], "actor": "reviewer", "decision": "approve"}]


@pytest.mark.parametrize("change", ["hash", "actor", "instructions", "handoff_bytes"])
def test_recovery_rejects_unbound_or_incomplete_authorization_without_child(tmp_path, change):
    store = Store(tmp_path)
    parent = failed_parent(store)
    reference = parent["data"]["handoff"]
    if change == "handoff_bytes":
        Path(reference["path"]).write_text("{}\n")
    with pytest.raises((StateConflict, ValueError)):
        engine(store).recover(parent["id"], "0" * 64 if change == "hash" else reference["hash"],
                              " " if change == "actor" else "reviewer", " " if change == "instructions" else "Rebuild")
    assert len(store.list_runs()) == 1
    assert not store.get(parent["id"])["data"].get("recovery_child")
    assert len([path for path in tmp_path.iterdir() if path.is_dir()]) == 1


def test_concurrent_recovery_commits_exactly_one_child_and_decision(tmp_path):
    store = Store(tmp_path)
    parent = failed_parent(store)
    barrier = Barrier(2)

    def attempt():
        barrier.wait()
        try:
            return store.recover(parent["id"], parent["data"]["handoff"]["hash"], "reviewer", "Rebuild", None,
                                 {"calls": 20, "active_seconds": 1800})["id"]
        except StateConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    children = [result for result in results if result]
    assert len(children) == 1 and len(store.list_runs()) == 2
    assert store.get(parent["id"])["data"]["recovery_child"] == children[0]
    assert sum(event["kind"] == "recovery_created" for event in store.events(parent["id"])) == 1
    with pytest.raises(StateConflict):
        recover(store, parent)


def test_recovery_uses_current_selection_and_still_requires_plan_approval(tmp_path):
    store = Store(tmp_path)
    original = accepted(store)
    bounded = engine(store, max_calls=0)
    parent = bounded.create("brownfield", baseline=original["id"])
    parent = asyncio.run(bounded.run(parent["id"]))
    current = accepted(store, original["id"])
    child = recover(store, parent)
    assert child["data"]["baseline"] == current["data"]["release"]
    assert child["data"]["expected_current"] == current["data"]["release"]["hash"]
    pending = asyncio.run(engine(store).run(child["id"]))
    assert pending["data"]["pending"]["action"] == "plan"
    assert store.selected("local")["run_id"] == current["id"]


def test_selection_race_and_missing_required_baseline_create_no_child(tmp_path):
    store = Store(tmp_path)
    original = accepted(store)
    parent = failed_parent(store)
    current = accepted(store, original["id"])
    with pytest.raises(StateConflict, match="Selected baseline changed"):
        store.recover(parent["id"], parent["data"]["handoff"]["hash"], "reviewer", "Rebuild",
                      original["data"]["release"], {"calls": 20, "active_seconds": 1800})
    assert len(store.list_runs()) == 3 and store.selected("local")["run_id"] == current["id"]
    isolated = Store(tmp_path / "isolated")
    run = isolated.create("brownfield", "local", "Extend accepted code", budget_limits={"calls": 0, "active_seconds": 1800})
    failed = asyncio.run(engine(isolated).run(run["id"]))
    with pytest.raises(StateConflict, match="current accepted baseline"):
        recover(isolated, failed)
    assert len(isolated.list_runs()) == 1


def test_child_lineage_survives_revision_and_cannot_form_a_retry_chain(tmp_path):
    store = Store(tmp_path)
    parent = failed_parent(store)
    child = recover(store, parent)
    lineage = child["data"]["recovery"]
    store.revise(child["id"], "Keep the API contract unchanged", alias_min=8, alias_max=8)
    assert store.get(child["id"])["data"]["recovery"] == lineage
    store.update(child["id"], 2, patch={"calls": 20})
    child = asyncio.run(engine(store).run(child["id"]))
    handoff = store.read_artifact(child["data"]["handoff"]["id"])
    assert handoff["permitted_action"] == "manual_review"
    with pytest.raises(StateConflict, match="cannot recover again"):
        recover(store, child)
    assert len(store.list_runs()) == 2


@pytest.mark.parametrize("failure", [PolicyError("Forbidden path"), StateConflict("Artifact integrity check failed"),
                                      GateFailure("Validation evidence changed"), RuntimeError("Unknown runner failure")])
def test_integrity_policy_and_unknown_failures_cannot_authorize_replacement(tmp_path, failure):
    store = Store(tmp_path)
    worker = engine(store)

    async def fail(run_id, revision):
        raise failure

    worker._workflow = fail
    run = worker.create("greenfield")
    parent = asyncio.run(worker.run(run["id"]))
    handoff = store.read_artifact(parent["data"]["handoff"]["id"])
    assert handoff["permitted_action"] == "manual_review"
    with pytest.raises(StateConflict, match="manual review"):
        recover(store, parent)
    assert len(store.list_runs()) == 1


def test_exhausted_provider_attempts_are_explicitly_recoverable(tmp_path):
    class UnavailableProvider(LocalProvider):
        async def respond(self, role, context, schema):
            raise ProviderFailure("Unit-test provider failure")

    store = Store(tmp_path)
    worker = Engine(store, UnavailableProvider(), EvidenceRunnerDouble())
    run = worker.create("greenfield")
    parent = asyncio.run(worker.run(run["id"]))
    handoff = store.read_artifact(parent["data"]["handoff"]["id"])
    assert handoff["failure_code"] == "node_attempts_exhausted"
    assert handoff["failed_node"] == "normalize" and parent["data"]["calls"] == 2
    assert recover(store, parent)["data"]["calls"] == 0


@pytest.mark.parametrize("verified", [False, True])
def test_repair_exhaustion_requires_verified_test_evidence(tmp_path, verified):
    class FailingRunner(EvidenceRunnerDouble):
        def validate(self, *args):
            report = super().validate(*args)
            report.update(passed=False, failures=1, summary="A deliberately failing runner double")
            if verified:
                report.update(protected_test_ids=["unit-double::test_failure"], evidence={"notice": "Verified test double"})
            else:
                report["diagnostics"] = {"notice": "Unverified envelope"}
            return report

    store = Store(tmp_path)
    runner = FailingRunner()
    worker = Engine(store, SyntheticRepairProvider(), runner)
    run = worker.create("greenfield")
    parent = asyncio.run(worker.run(run["id"]))
    handoff = store.read_artifact(parent["data"]["handoff"]["id"])
    assert parent["data"]["repairs"] == 2
    assert runner.validations == 3
    assert handoff["permitted_action"] == ("create_replacement" if verified else "manual_review")


def test_parallel_policy_failure_takes_precedence_over_budget_exhaustion():
    budget = RecoverableFailure("Budget exhausted", "call_budget_exhausted")
    for outcomes in ([budget, PolicyError("Forbidden path")], [PolicyError("Forbidden path"), budget]):
        with pytest.raises(PolicyError):
            Engine._raise_outcomes(outcomes)


def test_rejected_collection_diagnostics_can_drive_bounded_repair(tmp_path):
    class CollectionRunner(EvidenceRunnerDouble):
        def validate(self, *args):
            report = super().validate(*args)
            if self.validations == 1:
                report.update(passed=False, errors=1, summary="Collection failed",
                              diagnostics={"collection_errors": ["SyntaxError: invalid syntax"]})
            return report

    class RecordingProvider(SyntheticRepairProvider):
        diagnostics = None

        async def respond(self, role, context, schema):
            if "failure" in context:
                self.diagnostics = context["failure"]["diagnostics"]
            return await super().respond(role, context, schema)

    store, provider, runner = Store(tmp_path), RecordingProvider(), CollectionRunner()
    worker = Engine(store, provider, runner)
    run = worker.create("greenfield")
    result = asyncio.run(worker.run(run["id"]))
    assert result["status"] == "waiting_for_approval" and result["data"]["repairs"] == 1
    assert runner.validations == 2 and not result["data"].get("handoff")
    assert provider.diagnostics == {"collection_errors": ["SyntaxError: invalid syntax"]}
