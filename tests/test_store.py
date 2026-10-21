"""Transactional boundaries for persisted workflow state and human decisions."""

from pathlib import Path
import tempfile
import unittest

from orchestrator.store import StateConflict, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="agentic-store-test-")).resolve()
        self.store = Store(self.workspace)

    def create(self, mode="local"):
        return self.store.create("greenfield", mode, "Build the shortener")

    def gate(self, run, action="accept", expected_current=None):
        if expected_current is None:
            expected_current = run["data"].get("expected_current")
        record = self.store.artifact(run["id"], run["revision"], action, {"run": run["id"], "action": action})
        pending = {"action": action, "hash": record["hash"], "artifact_id": record["id"], "expected_current": expected_current}
        patch = {"pending": pending}
        if action == "accept":
            patch["release"] = record
        self.store.update(run["id"], run["revision"], status="waiting_for_approval", patch=patch)
        return record

    def accept(self, run, expected_current=None):
        release = self.gate(run, expected_current=expected_current)
        self.store.approve(run["id"], "accept", release["hash"], "human-reviewer")
        return release

    def test_approval_is_bound_to_current_action_hash_and_single_use_gate(self):
        run = self.create()
        plan = self.gate(run, action="plan")
        for action, digest in [("plan", "0" * 64), ("accept", plan["hash"])]:
            with self.subTest(action=action), self.assertRaises(StateConflict):
                self.store.approve(run["id"], action, digest, "human-reviewer")
        self.assertEqual(self.store.get(run["id"])["status"], "waiting_for_approval")
        self.assertIsNone(self.store.selected("local"))
        self.store.approve(run["id"], "plan", plan["hash"], "human-reviewer")
        approved = self.store.get(run["id"])
        self.assertEqual(approved["status"], "running")
        self.assertEqual(approved["data"]["approved_plan"], plan["hash"])
        self.assertIsNone(approved["data"]["pending"])
        with self.assertRaises(StateConflict):
            self.store.approve(run["id"], "plan", plan["hash"], "human-reviewer")
        decisions = [event for event in self.store.events(run["id"]) if event["kind"] == "human_decision"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["payload"]["hash"], plan["hash"])
        self.assertEqual(decisions[0]["payload"]["actor"], "human-reviewer")

    def test_rejection_stops_without_changing_accepted_candidate(self):
        prior = self.create()
        self.accept(prior)
        selected = self.store.selected("local")
        run = self.create()
        release = self.gate(run, expected_current=selected["hash"])
        for actor, decision in [(" ", "approve"), ("reviewer", "maybe")]:
            with self.subTest(actor=actor, decision=decision), self.assertRaises(ValueError):
                self.store.approve(run["id"], "accept", release["hash"], actor, decision)
        self.store.approve(run["id"], "accept", release["hash"], "human-reviewer", "reject")
        self.assertEqual(self.store.get(run["id"])["status"], "stopped")
        self.assertEqual(self.store.selected("local"), selected)
        self.assertFalse(any(event["kind"] == "rollback" for event in self.store.events(run["id"])))

    def test_revision_invalidates_old_nodes_and_approvals_but_retains_evidence(self):
        run = self.create()
        plan = self.gate(run, action="plan")
        self.store.approve(run["id"], "plan", plan["hash"], "human-reviewer")
        self.store.node(run["id"], 1, "implementation", "running", ["plan"])
        self.store.revise(run["id"], "Use custom aliases with a six-character minimum", alias_min=6)
        revised = self.store.get(run["id"])
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["data"]["alias_min"], 6)
        self.assertNotIn("approved_plan", revised["data"])
        self.assertEqual(self.store.nodes(run["id"], 1)["implementation"]["status"], "stale")
        self.assertEqual(self.store.nodes(run["id"], 2), {})
        with self.assertRaises(StateConflict):
            self.store.node(run["id"], 1, "implementation", "passed", result={"late": True})
        with self.assertRaises(StateConflict):
            self.store.update(run["id"], 1, status="succeeded")
        with self.store.connect() as database:
            valid = [row[0] for row in database.execute("SELECT valid FROM approvals WHERE run_id=?", (run["id"],))]
        self.assertEqual(valid, [0])
        self.assertEqual(self.store.read_artifact(plan["id"]), {"run": run["id"], "action": "plan"})

    def test_rejected_run_cannot_be_revived_by_late_completion(self):
        run = self.create()
        plan = self.gate(run, action="plan")
        self.store.approve(run["id"], "plan", plan["hash"], "human-reviewer", "reject")
        with self.subTest(operation="node completion"), self.assertRaises(StateConflict):
            self.store.node(run["id"], 1, "implementation", "passed", result={"late": True})
        with self.subTest(operation="run continuation"), self.assertRaises(StateConflict):
            self.store.update(run["id"], 1, status="running")

    def test_revision_invalidates_a_pending_approval(self):
        run = self.create()
        plan = self.gate(run, action="plan")
        self.store.revise(run["id"], "Use optional aliases")
        with self.assertRaises(StateConflict):
            self.store.approve(run["id"], "plan", plan["hash"], "human-reviewer")
        self.assertEqual(self.store.get(run["id"])["status"], "running")

    def test_acceptance_conflict_commits_stop_and_invalidates_gate(self):
        first, second = self.create(), self.create()
        first_release = self.gate(first)
        second_release = self.gate(second)
        self.store.approve(first["id"], "accept", first_release["hash"], "human-reviewer")
        with self.assertRaises(StateConflict):
            self.store.approve(second["id"], "accept", second_release["hash"], "human-reviewer")
        self.assertEqual(self.store.selected("local")["run_id"], first["id"])
        stopped = self.store.get(second["id"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertIsNone(stopped["data"]["pending"])
        self.assertTrue(stopped["data"]["stop_requested"])
        with self.store.connect() as database:
            count = database.execute("SELECT count(*) FROM approvals WHERE run_id=?", (second["id"],)).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertTrue(any(event["kind"] == "acceptance_conflict" for event in self.store.events(second["id"])))
        with self.assertRaises(StateConflict):
            self.store.revise(second["id"], "Try to continue obsolete work")

    def test_rollback_requires_accepted_target_and_matching_current_hash(self):
        first = self.create()
        first_release = self.accept(first)
        second, unaccepted = self.create(), self.create()
        second_release = self.accept(second, first_release["hash"])
        with self.assertRaises(StateConflict):
            self.store.rollback(unaccepted["id"], second_release["hash"], "human-reviewer")
        with self.assertRaises(StateConflict):
            self.store.rollback(first["id"], first_release["hash"], "human-reviewer")
        self.assertEqual(self.store.selected("local")["run_id"], second["id"])
        self.store.rollback(first["id"], second_release["hash"], "human-reviewer")
        self.assertEqual(self.store.selected("local")["hash"], first_release["hash"])
        with self.assertRaises(StateConflict):
            self.store.rollback(first["id"], first_release["hash"], "human-reviewer")
        rollbacks = [event for event in self.store.events(second["id"]) if event["kind"] == "rollback"]
        self.assertEqual(len(rollbacks), 1)
        self.assertEqual(rollbacks[0]["payload"], {"actor": "human-reviewer", "from": second_release["hash"], "to": first_release["hash"], "target_run": first["id"]})

    def test_artifact_tampering_is_detected(self):
        run = self.create()
        record = self.store.artifact(run["id"], 1, "validation", {"passed": False}, inputs=["candidate-hash"])
        self.assertEqual(self.store.read_artifact(record["id"]), {"passed": False})
        Path(record["path"]).write_text('{"passed": true}\n', encoding="utf-8")
        with self.assertRaisesRegex(StateConflict, "integrity"):
            self.store.read_artifact(record["id"])

    def test_stop_rejects_late_results_but_retains_audit_artifacts(self):
        run = self.create()
        self.store.node(run["id"], 1, "implementation", "running")
        self.store.stop(run["id"])
        with self.assertRaises(StateConflict):
            self.store.node(run["id"], 1, "implementation", "passed", result={"late": True})
        with self.assertRaises(StateConflict):
            self.store.update(run["id"], 1, status="succeeded", patch={"release": "late"})
        with self.assertRaises(StateConflict):
            self.store.approve(run["id"], "accept", "late", "human-reviewer")
        late = self.store.artifact(run["id"], 1, "implementation", {"late": True})
        self.assertEqual(self.store.read_artifact(late["id"]), {"late": True})
        self.assertEqual(self.store.get(run["id"])["status"], "stopped")
        self.assertIsNone(self.store.selected("local"))
        self.assertFalse(any(event["kind"] == "node_passed" for event in self.store.events(run["id"])))

    def test_lock_is_exclusive_per_run_and_released_after_error(self):
        run, other_run = self.create(), self.create()
        other_store = Store(self.workspace)
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with self.store.lock(run["id"]):
                with self.assertRaises(StateConflict):
                    with other_store.lock(run["id"]):
                        self.fail("A second process handle acquired the active run lock")
                with other_store.lock(other_run["id"]):
                    pass
                raise RuntimeError("interrupted")
        with other_store.lock(run["id"]):
            pass

    def test_attempts_count_dispatches_and_events_preserve_order(self):
        run = self.create()
        for status in ["running", "failed", "running", "passed"]:
            self.store.node(run["id"], 1, "implementation", status, ["plan"], {"status": status})
        node = self.store.nodes(run["id"])["implementation"]
        self.assertEqual(node["attempts"], 2)
        self.assertEqual(node["dependencies"], ["plan"])
        self.assertEqual(node["result"], {"status": "passed"})
        events = self.store.events(run["id"])
        self.assertEqual([event["kind"] for event in events[1:]], ["node_running", "node_failed", "node_running", "node_passed"])
        self.assertEqual([event["seq"] for event in events], sorted({event["seq"] for event in events}))


if __name__ == "__main__":
    unittest.main()
