"""Metric denominators and event timing semantics for local execution."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

from orchestrator.metrics import metrics
from orchestrator.store import Store


class Timeline:
    def __init__(self, monkeypatch):
        self.second = 0
        base = datetime(2030, 1, 1, tzinfo=timezone.utc)
        monkeypatch.setattr("orchestrator.store.now", lambda: (base + timedelta(seconds=self.second)).isoformat())
        self.store = Store(Path(tempfile.mkdtemp(prefix="agentic-metrics-")))

    def create(self):
        return self.store.create("greenfield", "local", "Synthetic metric scenario")

    def event(self, run, second, kind, payload=None, revision=1):
        self.second = second
        self.store.event(run["id"], revision, kind, payload or {})

    def finish(self, run, second, status, active=0):
        self.second = second
        self.store.update(run["id"], run["revision"], status=status, patch={"active_seconds": active})

    def accept(self, run, second):
        self.second = second
        release = self.store.artifact(run["id"], 1, "release", {"run": run["id"]})
        selected = self.store.selected(run["mode"])
        pending = {"action": "accept", "hash": release["hash"], "artifact_id": release["id"], "expected_current": selected["hash"] if selected else None}
        self.store.update(run["id"], 1, status="waiting_for_approval", patch={"pending": pending, "release": release})
        self.store.approve(run["id"], "accept", release["hash"], "synthetic-human")


def test_empty_groups_report_unavailable_rates(monkeypatch):
    result = metrics(Timeline(monkeypatch).store)
    assert set(result["groups"]) == {"local"}
    for group in result["groups"].values():
        assert group["runs"] == 0
        for key in ("success_rate", "retry_frequency", "rollback_frequency", "mttr_proxy_seconds", "mean_end_to_end_seconds"):
            assert group[key] == "N/A"


def test_success_rate_requires_an_acceptance_decision(monkeypatch):
    timeline = Timeline(monkeypatch)
    accepted = timeline.create()
    timeline.accept(accepted, 10)
    failed = timeline.create()
    timeline.finish(failed, 20, "failed")
    unapproved = timeline.create()
    timeline.finish(unapproved, 30, "succeeded")
    timeline.create()
    group = metrics(timeline.store)["groups"]["local"]
    assert group["runs"] == 4
    assert group["terminal_runs"] == 3
    assert group["accepted_runs"] == 1
    assert group["success_rate"] == 1 / 3
    assert group["statuses"]["failed"] == 1


def test_retries_are_per_node_revision_and_repairs_are_separate(monkeypatch):
    timeline = Timeline(monkeypatch)
    run = timeline.create()
    for second, revision, node in [(1, 1, "build"), (2, 1, "build"), (3, 1, "repair_1"), (4, 2, "build")]:
        timeline.event(run, second, "node_running", {"node": node}, revision)
    timeline.event(run, 5, "repair_applied")
    group = metrics(timeline.store)["groups"]["local"]
    assert group["started_attempts"] == 4
    assert group["additional_attempts"] == 1
    assert group["retry_frequency"] == 0.25
    assert group["repairs"] == 1


def test_recovery_pairs_first_failure_and_does_not_cross_revisions(monkeypatch):
    timeline = Timeline(monkeypatch)
    run = timeline.create()
    for second, kind, revision in [(1, "recovery_completed", 1), (10, "validation_failed", 1), (15, "validation_failed", 1), (30, "recovery_completed", 1), (40, "validation_failed", 1), (50, "validation_failed", 2), (60, "recovery_completed", 2)]:
        timeline.event(run, second, kind, revision=revision)
    group = metrics(timeline.store)["groups"]["local"]
    assert group["recovery_seconds"] == [20, 10]
    assert group["recovery_samples"] == 2
    assert group["mttr_proxy_seconds"] == 15
    assert group["unresolved_failure_episodes"] == 1


def test_terminal_latency_excludes_later_accounting_and_rollback(monkeypatch):
    timeline = Timeline(monkeypatch)
    run = timeline.create()
    timeline.event(run, 5, "validation_failed")
    timeline.finish(run, 10, "failed", active=3)
    timeline.second = 100
    timeline.store.update(run["id"], 1, patch={"active_seconds": 4})
    group = metrics(timeline.store)["groups"]["local"]
    assert group["terminal_durations"][0] == {"run_id": run["id"], "end_to_end_seconds": 10, "active_seconds": 4, "waiting_seconds": 6}
    assert group["unresolved_failure_episodes"] == 1
    timeline.second = 110
    timeline.store.update(run["id"], 1, status="running")
    timeline.accept(run, 130)
    timeline.event(run, 1000, "rollback", {"from": "new", "to": "prior"})
    group = metrics(timeline.store)["groups"]["local"]
    assert group["mean_end_to_end_seconds"] == 130
    assert group["rollbacks"] == 1
    assert group["rollback_frequency"] == 1
    assert group["success_rate"] == 1
