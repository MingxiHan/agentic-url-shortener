"""Engine gate tests use explicit doubles; no generated code is executed."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from orchestrator.artifacts import PolicyError, sha256_bytes
from orchestrator.engine import Engine, GateFailure, ProviderFailure
from orchestrator.models import ChangeSet, Plan, Requirement, Review
from orchestrator.local_provider import LocalProvider
from orchestrator.store import StateConflict, Store, digest


class EvidenceRunnerDouble:
    mode = "local"

    def __init__(self):
        self.validations = 0

    def preflight(self):
        return {"notice": "Unit-test double; no service execution"}

    def validate(self, candidate, output_dir, scenario, alias_min, alias_max, stopped):
        self.validations += 1
        output_dir.mkdir(parents=True)
        evidence = output_dir / "checks.txt"
        evidence.write_text("Unit-test evidence double\n", encoding="utf-8")
        result = {"passed": True, "summary": "Unit-test double result", "tests": 1, "failures": 0, "errors": 0, "skipped": 0,
                  "artifacts": {"checks.txt": {"path": str(evidence), "sha256": sha256_bytes(evidence.read_bytes())}}}
        (output_dir / "report.json").write_text(json.dumps(result), encoding="utf-8")
        return result


class RuntimeDataRunner(EvidenceRunnerDouble):
    def validate(self, candidate, output_dir, scenario, alias_min, alias_max, stopped):
        result = super().validate(candidate, output_dir, scenario, alias_min, alias_max, stopped)
        (output_dir / "runtime").mkdir()
        (output_dir / "runtime" / "test.sqlite").write_bytes(b"Mutable test database")
        (output_dir / "stdout.log").write_text("Validation output\n", encoding="utf-8")
        return result


class CollidingTaskProvider(LocalProvider):
    async def respond(self, role, context, schema):
        if schema is Plan:
            output = Plan(
                rationale="Valid ordered writers with IDs that must not alias internal call nodes.",
                impacted_paths=["service/shortener/app.py"], risks=[],
                tasks=[
                    {"id": "task_build", "kind": "implementation", "description": "First write", "dependencies": []},
                    {"id": "task_build_call", "kind": "implementation", "description": "Second write", "dependencies": ["task_build"]},
                    {"id": "task_tests", "kind": "tests", "description": "Independent tests", "dependencies": []},
                ],
            )
            return output, {"provider": "unit-test-double"}
        if schema is ChangeSet and role == "builder":
            operation = "core" if context["task"]["id"] == "task_build" else "verify_existing"
            context = {**context, "task": {**context["task"], "id": "task_" + operation}}
        return await super().respond(role, context, schema)


class ParallelBranchProvider(LocalProvider):
    def __init__(self):
        super().__init__()
        self.entered = set()
        self.both_started = asyncio.Event()

    async def respond(self, role, context, schema):
        if schema is ChangeSet and role in {"builder", "verifier"}:
            self.entered.add(role)
            if self.entered == {"builder", "verifier"}:
                self.both_started.set()
            await asyncio.wait_for(self.both_started.wait(), timeout=1)
        return await super().respond(role, context, schema)


class RetryOnceProvider(LocalProvider):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.contexts = []

    async def respond(self, role, context, schema):
        self.calls += 1
        self.contexts.append(json.loads(json.dumps(context)))
        if self.calls == 1:
            raise ProviderFailure("Unit-test transient provider failure")
        return await super().respond(role, context, schema)


class SyntheticRepairProvider(LocalProvider):
    """Exercise repair control flow without claiming the local rules repair defects."""

    async def respond(self, role, context, schema):
        if schema is ChangeSet and role == "builder" and "failure" in context:
            path = "service/shortener/app.py"
            return ChangeSet(rationale="Explicit unchanged-source repair double", changes=[{
                "path": path, "previous_hash": context["file_hashes"][path], "content": context["files"][path],
            }]), {"provider": "unit-test-double"}
        return await super().respond(role, context, schema)


class RepairContextProvider(LocalProvider):
    def __init__(self):
        super().__init__()
        self.repairs = []
        self.reviews = []

    async def respond(self, role, context, schema):
        if role == "reviewer":
            self.reviews.append(json.loads(json.dumps(context)))
            return Review(summary="Explicit review double for repair context testing", findings=[]), {"provider": "unit-test-double"}
        if role == "builder" and "failure" in context:
            self.repairs.append(json.loads(json.dumps(context)))
            path = "service/shortener/app.py"
            output = ChangeSet(rationale="Repair only the current failing source", changes=[{
                "path": path, "previous_hash": context["file_hashes"][path],
                "content": context["files"][path].replace("REDIRECT_STATUS = 307", "REDIRECT_STATUS = 302"),
            }])
            return output, {"provider": "unit-test-double", "prompt_version": "1"}
        output, metadata = await super().respond(role, context, schema)
        if role == "builder" and schema is ChangeSet:
            for change in output.changes:
                if change.path == "service/shortener/app.py":
                    change.content = change.content.replace("REDIRECT_STATUS = 302", "REDIRECT_STATUS = 307")
        return output, metadata


class RepairResumeRunner(EvidenceRunnerDouble):
    def __init__(self):
        super().__init__()
        self.interrupted = False

    def validate(self, candidate, output_dir, scenario, alias_min, alias_max, stopped):
        result = super().validate(candidate, output_dir, scenario, alias_min, alias_max, stopped)
        source = (candidate / "service/shortener/app.py").read_text(encoding="utf-8")
        if "REDIRECT_STATUS = 307" in source:
            result.update(passed=False, failures=1, summary="The redirect status must equal 302",
                          protected_test_ids=["unit-test-double::redirect"],
                          evidence={"failed_assertion": "assert redirect.status_code == 302"})
            (output_dir / "report.json").write_text(json.dumps(result), encoding="utf-8")
        elif not self.interrupted:
            self.interrupted = True
            raise RuntimeError("Unit-test interruption after source repair")
        return result


class EngineGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="agentic-engine-test-")).resolve()
        self.store = Store(self.workspace)
        self.runner = EvidenceRunnerDouble()
        self.engine = Engine(self.store, LocalProvider(), self.runner)

    async def pending(self, engine=None):
        engine = engine or self.engine
        run = engine.create("greenfield")
        result = await engine.run(run["id"])
        self.assertEqual(result["status"], "waiting_for_approval", result["data"].get("error"))
        self.assertEqual(result["data"]["pending"]["action"], "accept")
        return result

    async def advance_change(self, run):
        result = await self.engine.run(run["id"])
        self.assertEqual(result["data"]["pending"]["action"], "plan")
        self.engine.approve(run["id"], "plan", result["data"]["pending"]["hash"], "human-reviewer")
        result = await self.engine.run(run["id"])
        self.assertEqual(result["status"], "waiting_for_approval", result["data"].get("error"))
        self.assertEqual(result["data"]["pending"]["action"], "accept")
        return result

    def accept(self, run):
        self.engine.approve(run["id"], "accept", run["data"]["pending"]["hash"], "human-reviewer")

    def assert_acceptance_conflict(self, run, selected):
        with self.assertRaisesRegex(StateConflict, "new run"):
            self.accept(run)
        stopped = self.store.get(run["id"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertTrue(stopped["data"]["stop_requested"])
        self.assertIsNone(stopped["data"]["pending"])
        self.assertEqual(self.store.selected("local")["run_id"], selected["id"])
        self.assertTrue(any(event["kind"] == "acceptance_conflict" for event in self.store.events(run["id"])))
        with self.store.connect() as database:
            valid = database.execute("SELECT count(*) FROM approvals WHERE run_id=? AND valid=1", (run["id"],)).fetchone()[0]
        self.assertEqual(valid, 0)

    async def test_valid_task_ids_do_not_collide_with_internal_model_call_nodes(self):
        engine = Engine(self.store, CollidingTaskProvider(), self.runner)
        run = await self.pending(engine)
        nodes = self.store.nodes(run["id"])
        for task_id in ("task_build", "task_build_call", "task_tests"):
            self.assertEqual(nodes[task_id]["status"], "passed")
            self.assertIn("candidate", nodes[task_id]["result"])

    async def test_independent_builder_and_verifier_really_run_concurrently(self):
        provider = ParallelBranchProvider()
        engine = Engine(self.store, provider, self.runner)
        await self.pending(engine)
        self.assertEqual(provider.entered, {"builder", "verifier"})
        self.assertEqual(self.runner.validations, 1)

    async def test_greenfield_label_cannot_skip_approval_for_a_baseline_change(self):
        baseline = await self.pending()
        self.engine.approve(baseline["id"], "accept", baseline["data"]["pending"]["hash"], "human-reviewer")
        run = self.engine.create("greenfield", baseline=baseline["id"])
        result = await self.engine.run(run["id"])
        self.assertEqual(result["status"], "waiting_for_approval")
        self.assertEqual(result["data"]["pending"]["action"], "plan")
        nodes = self.store.nodes(run["id"])
        self.assertEqual(nodes["task_verify_existing"]["status"], "pending")
        self.assertEqual(nodes["task_contract_tests"]["status"], "pending")
        self.assertNotIn("call:task_verify_existing", nodes)
        self.assertEqual(self.runner.validations, 1)

    async def test_candidate_tamper_prevents_acceptance(self):
        run = await self.pending()
        release = self.store.read_artifact(run["data"]["release"]["id"])
        candidate = Path(release["candidate"]["path"])
        with (candidate / "service/shortener/app.py").open("a", encoding="utf-8") as handle:
            handle.write("\n# Changed after validation.\n")
        with self.assertRaises(PolicyError):
            self.engine.approve(run["id"], "accept", run["data"]["pending"]["hash"], "human-reviewer")
        self.assertIsNone(self.store.selected("local"))
        self.assertEqual(self.store.get(run["id"])["status"], "waiting_for_approval")

    async def test_changed_evidence_prevents_acceptance(self):
        run = await self.pending()
        release = self.store.read_artifact(run["data"]["release"]["id"])
        evidence = self.store.read_artifact(release["validation"]["id"])
        (Path(evidence["evidence_dir"]) / "checks.txt").write_text("Changed after validation\n", encoding="utf-8")
        with self.assertRaises(GateFailure):
            self.engine.approve(run["id"], "accept", run["data"]["pending"]["hash"], "human-reviewer")
        self.assertIsNone(self.store.selected("local"))

    async def test_corrupted_evidence_can_still_be_rejected(self):
        run = await self.pending()
        release = self.store.read_artifact(run["data"]["release"]["id"])
        evidence = self.store.read_artifact(release["validation"]["id"])
        (Path(evidence["evidence_dir"]) / "checks.txt").write_text("Corrupted evidence\n", encoding="utf-8")
        self.engine.approve(run["id"], "accept", run["data"]["pending"]["hash"], "human-reviewer", "reject")
        self.assertEqual(self.store.get(run["id"])["status"], "stopped")
        self.assertIsNone(self.store.selected("local"))

    async def test_old_revision_approval_cannot_accept_a_new_release(self):
        run = await self.pending()
        old_hash = run["data"]["pending"]["hash"]
        self.store.revise(run["id"], "Create a core URL shortener with redirects and counts.")
        revised = await self.engine.run(run["id"])
        self.assertEqual(revised["status"], "waiting_for_approval")
        self.assertEqual(revised["revision"], 2)
        self.assertNotEqual(revised["data"]["pending"]["hash"], old_hash)
        with self.assertRaises(StateConflict):
            self.engine.approve(run["id"], "accept", old_hash, "human-reviewer")
        self.assertIsNone(self.store.selected("local"))

    async def test_call_budget_is_shared_by_parallel_branches_and_handoff_is_terminal(self):
        engine = Engine(self.store, LocalProvider(), self.runner, max_calls=3)
        run = engine.create("greenfield")
        first = await engine.run(run["id"])
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["data"]["calls"], 3)
        self.assertIn("budget", first["data"]["error"])
        with self.assertRaisesRegex(StateConflict, "terminal"):
            await engine.run(run["id"])
        self.assertEqual(self.store.get(run["id"])["data"]["calls"], 3)
        self.assertEqual(self.runner.validations, 0)
        self.assertIsNone(self.store.selected("local"))

    async def test_exhausted_active_budget_prevents_model_calls(self):
        engine = Engine(self.store, LocalProvider(), self.runner, max_seconds=1)
        run = engine.create("greenfield")
        self.store.update(run["id"], 1, patch={"active_seconds": 1.0})
        result = await engine.run(run["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["data"]["calls"], 0)
        self.assertEqual(self.runner.validations, 0)

    async def test_cached_nodes_cannot_bypass_active_budget_on_resume(self):
        run = await self.pending()
        # Cached work survived an interruption before final gating; the run is resumable.
        self.store.update(run["id"], run["revision"], status="running", patch={"pending": None, "active_seconds": self.engine.max_seconds})
        result = await self.engine.run(run["id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("budget", result["data"]["error"])
        self.assertIsNone(result["data"].get("pending"))
        self.assertEqual(self.runner.validations, 1)

    async def test_cached_model_output_rejects_changed_context(self):
        run = self.engine.create("greenfield")
        context = self.engine._context(run["id"])
        await self.engine._invoke(run["id"], 1, "normalize", "normalize", Requirement, context)
        with self.assertRaisesRegex(GateFailure, "Cached role inputs changed"):
            await self.engine._invoke(run["id"], 1, "normalize", "normalize", Requirement, {**context, "requirement": "A different requirement"})
        self.assertEqual(self.store.get(run["id"])["data"]["calls"], 1)

    async def test_successful_correction_retry_can_reuse_original_inputs(self):
        provider = RetryOnceProvider()
        engine = Engine(self.store, provider, self.runner)
        run = engine.create("greenfield")
        context = engine._context(run["id"])
        output, record = await engine._invoke(run["id"], 1, "normalize", "normalize", Requirement, context)
        cached, cached_record = await engine._invoke(run["id"], 1, "normalize", "normalize", Requirement, context)
        self.assertEqual(cached, output)
        self.assertEqual(cached_record, record)
        self.assertEqual(provider.calls, 2)
        evidence = self.store.read_artifact(record["id"])
        self.assertNotEqual(evidence["input_hash"], evidence["request_hash"])
        self.assertEqual(evidence["input_hash"], digest(context))
        self.assertEqual(evidence["input_context"], provider.contexts[-1])
        self.assertEqual(evidence["request_hash"], digest(evidence["input_context"]))
        self.assertEqual(evidence["role"], "normalize")
        self.assertEqual(evidence["prompt"], {"output_schema": "Requirement"})
        self.assertEqual(evidence["metadata"]["provider"], "local_rules")
        self.assertEqual(evidence["metadata"]["model_calls"], 0)
        self.assertIn("orchestrator/local_provider.py", evidence["metadata"]["rule_sources"])

    async def test_cached_validation_rejects_a_different_candidate(self):
        run = await self.pending()
        candidate = run["data"]["candidate"]
        path = "service/shortener/app.py"
        previous = (Path(candidate["path"]) / path).read_text(encoding="utf-8")
        changes = ChangeSet(rationale="A distinct candidate requires its own validation", changes=[{"path": path, "previous_hash": sha256_bytes(previous.encode("utf-8")), "content": previous + "\n# A different candidate.\n"}])
        different = self.engine._apply(run["id"], candidate, changes, "builder")
        with self.assertRaisesRegex(GateFailure, "Cached validation inputs changed"):
            await self.engine._validate(run["id"], 1, "validate_0", different)
        self.assertEqual(self.runner.validations, 1)

    async def test_cached_validation_rechecks_evidence(self):
        run = await self.pending()
        record = self.store.nodes(run["id"])["validate_0"]["result"]
        evidence = self.store.read_artifact(record["id"])
        (Path(evidence["evidence_dir"]) / "checks.txt").write_text("Changed cached evidence\n", encoding="utf-8")
        with self.assertRaisesRegex(GateFailure, "evidence changed"):
            await self.engine._validate(run["id"], 1, "validate_0", run["data"]["candidate"])

    async def test_cached_results_respect_a_stop(self):
        run = await self.pending()
        self.store.stop(run["id"])
        with self.assertRaises(StateConflict):
            await self.engine._invoke(run["id"], 1, "normalize", "normalize", Requirement, self.engine._context(run["id"]))
        with self.assertRaises(StateConflict):
            await self.engine._validate(run["id"], 1, "validate_0", run["data"]["candidate"])

    async def test_selection_change_before_final_gate_cannot_overwrite_newer_release(self):
        baseline = await self.pending()
        self.accept(baseline)
        first = self.engine.create("brownfield", baseline=baseline["id"])
        second = self.engine.create("brownfield", baseline=baseline["id"])
        expected = first["data"]["expected_current"]
        self.store.revise(second["id"], "Add optional link expiration while preserving existing links and counts.")
        self.assertEqual(self.store.get(second["id"])["data"]["expected_current"], expected)
        first = await self.advance_change(first)
        self.accept(first)
        second = await self.advance_change(second)
        self.assertEqual(second["data"]["pending"]["expected_current"], expected)
        self.assert_acceptance_conflict(second, first)

    async def test_selection_change_after_final_gate_stops_stale_run(self):
        baseline = await self.pending()
        self.accept(baseline)
        first = self.engine.create("brownfield", baseline=baseline["id"])
        second = self.engine.create("brownfield", baseline=baseline["id"])
        first = await self.advance_change(first)
        second = await self.advance_change(second)
        self.accept(first)
        self.assert_acceptance_conflict(second, first)

    async def test_only_selected_baseline_can_start_a_change_and_rollback_restores_it(self):
        baseline = await self.pending()
        self.accept(baseline)
        changed = await self.advance_change(self.engine.create("brownfield", baseline=baseline["id"]))
        self.accept(changed)
        before = len(self.store.list_runs())
        with self.assertRaisesRegex(StateConflict, "no longer selected"):
            self.engine.create("brownfield", baseline=baseline["id"])
        self.assertEqual(len(self.store.list_runs()), before)
        self.engine.rollback(baseline["id"], changed["data"]["pending"]["hash"], "human-reviewer")
        restored = self.engine.create("ambiguous", baseline=baseline["id"])
        self.assertEqual(restored["data"]["expected_current"], baseline["data"]["pending"]["hash"])

    async def test_fault_injection_is_rejected_before_a_run_or_call(self):
        with self.assertRaisesRegex(GateFailure, "not supported"):
            self.engine.create("greenfield", inject_fault=True)
        self.assertEqual(self.store.list_runs(), [])
        self.assertEqual(self.runner.validations, 0)

    async def test_mutable_runtime_files_are_not_release_evidence(self):
        engine = Engine(self.store, LocalProvider(), RuntimeDataRunner())
        run = await self.pending(engine)
        release = self.store.read_artifact(run["data"]["release"]["id"])
        evidence = self.store.read_artifact(release["validation"]["id"])
        self.assertEqual(set(evidence["evidence_hashes"]), {"checks.txt", "report.json", "stdout.log"})
        (Path(evidence["evidence_dir"]) / "runtime" / "test.sqlite").write_bytes(b"Changed runtime database")
        engine.verify_release(run["data"]["release"]["id"])

    async def test_exported_evidence_must_be_a_matching_direct_regular_file(self):
        directory = self.workspace / "evidence"
        directory.mkdir()
        (directory / "report.json").write_text("{}", encoding="utf-8")
        (directory / "runtime").mkdir()
        nested = directory / "runtime" / "checks.txt"
        nested.write_text("Nested output", encoding="utf-8")
        with self.assertRaisesRegex(GateFailure, "direct child"):
            self.engine._capture_evidence(directory, {"artifacts": {"runtime/checks.txt": {"path": str(nested), "sha256": sha256_bytes(nested.read_bytes())}}})
        linked = directory / "linked.txt"
        linked.symlink_to(nested)
        with self.assertRaisesRegex(GateFailure, "regular file"):
            self.engine._capture_evidence(directory, {"artifacts": {"linked.txt": {"path": str(linked), "sha256": sha256_bytes(nested.read_bytes())}}})

    async def test_repair_uses_current_source_hashes_and_unknown_interruption_hands_off(self):
        provider, runner = RepairContextProvider(), RepairResumeRunner()
        engine = Engine(self.store, provider, runner)
        run = engine.create("greenfield")
        interrupted = await engine.run(run["id"])
        self.assertEqual(interrupted["status"], "failed")
        self.assertIn("interruption after source repair", interrupted["data"]["error"])
        self.assertEqual(interrupted["data"]["repairs"], 1)
        self.assertEqual(len(provider.repairs), 1)
        repair = provider.repairs[0]
        path = "service/shortener/app.py"
        self.assertIn("REDIRECT_STATUS = 307", repair["files"][path])
        self.assertEqual(repair["file_hashes"][path], sha256_bytes(repair["files"][path].encode()))
        self.assertEqual(repair["failure"]["evidence"]["failed_assertion"], "assert redirect.status_code == 302")
        test_path = "generated_tests/test_rule_contract.py"
        with self.assertRaisesRegex(StateConflict, "terminal"):
            await engine.run(run["id"])
        handoff = self.store.read_artifact(interrupted["data"]["handoff"]["id"])
        self.assertEqual(handoff["permitted_action"], "manual_review")
        self.assertEqual(len(provider.repairs), 1)
        self.assertEqual(runner.validations, 2)
        candidate = Path(interrupted["data"]["candidate"]["path"])
        self.assertEqual((candidate / test_path).read_text(), repair["files"][test_path])
        self.assertIn("REDIRECT_STATUS = 302", provider.reviews[-1]["files"][path])
        self.assertEqual(provider.reviews[0]["baseline_files"], provider.reviews[-1]["baseline_files"])
        record = self.store.nodes(run["id"])["repair_1"]["result"]
        evidence = self.store.read_artifact(record["id"])
        self.assertEqual(evidence["input_context"], repair)
        self.assertEqual(evidence["role"], "builder")
        self.assertEqual(evidence["request_hash"], digest(repair))


if __name__ == "__main__":
    unittest.main()
