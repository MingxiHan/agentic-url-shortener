"""Explicit stage gates around a bounded local task dependency graph."""

import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path

from pydantic import ValidationError

from .artifacts import PolicyError, apply_changes, manifest, snapshot_files, verify_manifest
from .contracts import SCENARIOS, contract
from .models import ChangeSet, Documentation, Plan, Requirement, Review
from .store import StateConflict, Store, digest

ROOT = Path(__file__).resolve().parents[1]


class ProviderFailure(RuntimeError):
    """An explicitly retryable role execution failure."""


class GateFailure(RuntimeError):
    pass


class RecoverableFailure(GateFailure):
    """Only explicit bounded exhaustion may authorize a replacement run."""

    def __init__(self, message, code, node=None):
        super().__init__(message)
        self.code, self.node = code, node


class Engine:
    def __init__(self, store: Store, provider, runner, max_calls=20, max_seconds=1800):
        if getattr(runner, "mode", None) != provider.mode:
            raise ValueError("Provider and runner execution modes must match")
        if provider.mode != "local":
            raise ValueError("Only local execution is supported")
        self.store, self.provider, self.runner = store, provider, runner
        self.max_calls, self.max_seconds = max_calls, max_seconds
        self.started = None
        self.active_before = 0.0
        self.abort = False

    def create(self, scenario, requirement=None, baseline=None, inject_fault=False):
        if scenario not in SCENARIOS:
            raise ValueError("Unknown scenario")
        if inject_fault:
            raise GateFailure("Predefined fault injection is not supported")
        if scenario != "greenfield" and not baseline:
            raise ValueError("Brownfield and ambiguous runs require an accepted baseline run")
        baseline_record = None
        if baseline:
            prior = self.store.get(baseline)
            if prior["mode"] != self.provider.mode or prior["status"] != "succeeded":
                raise GateFailure("Baseline must be accepted in the same execution mode")
            baseline_record = prior["data"]["release"]
            self.verify_release(baseline_record["id"])
        return self.store.create(scenario, self.provider.mode, requirement or SCENARIOS[scenario], baseline_record, inject_fault,
                                 {"calls": self.max_calls, "active_seconds": self.max_seconds})

    def _check(self, run_id, revision):
        run = self.store.get(run_id)
        if run["revision"] != revision or run["data"].get("stop_requested") or run["data"].get("handoff") or self.abort:
            raise StateConflict("Execution stopped or superseded; result cannot pass a gate")
        elapsed = (time.monotonic() - self.started) if self.started is not None else 0
        if self.active_before + elapsed >= run["data"].get("budget_limits", {}).get("active_seconds", self.max_seconds):
            raise RecoverableFailure("Active execution budget exhausted", "active_budget_exhausted")
        return run

    def _context(self, run_id, files=None):
        run = self.store.get(run_id)
        data = run["data"]
        return {
            "mode": run["mode"], "scenario": run["scenario"], "revision": run["revision"],
            "requirement": data["requirement"], "answers": data["answers"],
            **({"recovery_instructions": data["recovery"]["instructions"]} if data.get("recovery") else {}),
            "contract": contract(run["scenario"], data["alias_min"], data["alias_max"]),
            "files": files or {}, "file_hashes": {path: hashlib.sha256(content.encode()).hexdigest() for path, content in (files or {}).items()},
        }

    async def _invoke(self, run_id, revision, node_id, role, schema, context, dependencies=(), inputs=()):
        self._check(run_id, revision)
        input_hash = digest(context)
        previous = self.store.nodes(run_id, revision).get(node_id)
        if previous and previous["status"] == "passed":
            record = previous["result"]
            cached = self.store.read_artifact(record["id"])
            if cached.get("input_hash") != input_hash:
                raise GateFailure("Cached role inputs changed; create a new requirement revision")
            output = schema.model_validate(cached["output"])
            self._check(run_id, revision)
            return output, record
        attempts = previous["attempts"] if previous else 0
        last_error = None
        while attempts < 2:
            run = self._check(run_id, revision)
            if run["data"]["calls"] >= run["data"].get("budget_limits", {}).get("calls", self.max_calls):
                raise RecoverableFailure("Role dispatch budget exhausted", "call_budget_exhausted", node_id)
            self.store.update(run_id, revision, patch={"calls": run["data"]["calls"] + 1})
            self.store.node(run_id, revision, node_id, "running", dependencies)
            self.store.event(run_id, revision, "call_started", {"node": node_id, "role": role, "mode": run["mode"]})
            started = time.monotonic()
            input_context = json.loads(json.dumps(context))
            prompt = {"output_schema": schema.__name__}
            task = asyncio.create_task(self.provider.respond(role, context, schema))
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=0.2)
                    self._check(run_id, revision)
                    if time.monotonic() - started > 90:
                        raise TimeoutError("Role dispatch timeout")
                output, metadata = task.result()
                output = schema.model_validate(output)
                self._check(run_id, revision)
                record = self.store.artifact(run_id, revision, node_id, {
                    "output": output.model_dump(), "metadata": metadata,
                    "role": role, "input_context": input_context, "prompt": prompt,
                    "input_hash": input_hash, "request_hash": digest(input_context),
                    "duration_seconds": time.monotonic() - started,
                }, inputs)
                self.store.node(run_id, revision, node_id, "passed", dependencies, record)
                return output, record
            except (StateConflict, PolicyError, GateFailure, asyncio.CancelledError):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            except Exception as error:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if not isinstance(error, (ProviderFailure, ValidationError, TimeoutError)):
                    raise
                # Store validation diagnostics, not provider request bodies or credentials.
                last_error = f"{type(error).__name__}: invalid structured output or unavailable provider"
                if isinstance(error, ProviderFailure):
                    last_error = str(error)
                self.store.event(run_id, revision, "call_failed", {"node": node_id, "error": last_error})
                self.store.node(run_id, revision, node_id, "failed", dependencies)
                attempts += 1
                if attempts < 2:
                    self.store.event(run_id, revision, "retry", {"node": node_id})
                    context = {**context, "correction": "The prior call failed output validation. Return a complete schema-conforming result with a valid dependency graph."}
                    await asyncio.sleep(0.25)
        raise RecoverableFailure(last_error or "Node attempt budget exhausted", "node_attempts_exhausted", node_id)

    def _baseline(self, run_id, revision):
        run = self.store.get(run_id)
        if run["data"].get("base"):
            base = run["data"]["base"]
            verify_manifest(Path(base["path"]), base["manifest"])
            return base
        if run["data"]["baseline"]:
            release = self.verify_release(run["data"]["baseline"]["id"])
            base = release["candidate"]
        else:
            path = self.store.root / run_id / f"base-{revision}"
            if path.exists():
                path = self.store.root / run_id / f"base-{revision}-{uuid.uuid4().hex[:6]}"
            (path / "service" / "shortener").mkdir(parents=True)
            (path / "generated_tests").mkdir()
            for source in sorted((ROOT / "skeleton" / "shortener").glob("*.py")):
                with (path / "service" / "shortener" / source.name).open("x") as handle:
                    handle.write(source.read_text())
            base = {"path": str(path), "manifest": manifest(path)}
        self.store.update(run_id, revision, patch={"base": base})
        self.store.node(run_id, revision, "inspect", "passed", ["normalize"], {"files": list(base["manifest"]["files"]), "baseline_hash": base["manifest"]["hash"]})
        return base

    def _gate(self, run_id, revision, action, hash_value, artifact_id, explanation):
        run = self._check(run_id, revision)
        pending = {"action": action, "hash": hash_value, "artifact_id": artifact_id,
                   "expected_current": run["data"].get("expected_current"), "explanation": explanation}
        self.store.update(run_id, revision, status="waiting_for_approval", patch={"pending": pending}, kind="approval_requested")

    async def run(self, run_id):
        with self.store.lock(run_id):
            run = self.store.get(run_id)
            if run["mode"] != self.provider.mode:
                raise GateFailure("Execution mode cannot change on resume")
            if run["data"].get("handoff"):
                raise StateConflict("Handed-off runs are terminal; inspect the handoff and use recover if permitted")
            if run["status"] == "failed":
                raise StateConflict("Failed runs are terminal even without a handoff; inspect the retained error and evidence")
            if run["status"] in ("succeeded", "stopped", "waiting_for_approval", "waiting_for_input"):
                return run
            revision = run["revision"]
            self.started, self.active_before, self.abort = time.monotonic(), run["data"]["active_seconds"], False
            failure = None
            try:
                self._check(run_id, revision)
                # Check the configured execution boundary before dispatching any provider work.
                preflight = self.runner.preflight()
                self.provider.preflight()
                self.store.event(run_id, revision, "preflight_passed", preflight)
                self.store.update(run_id, revision, status="running")
                for node_id, node in self.store.nodes(run_id, revision).items():
                    if node["status"] == "running":
                        self.store.event(run_id, revision, "node_interrupted", {"node": node_id})
                        self.store.node(run_id, revision, node_id, "failed", node["dependencies"])
                await self._workflow(run_id, revision)
            except (Exception, asyncio.CancelledError) as error:
                failure = error
                self.abort = True
                current = self.store.get(run_id)
                if current["revision"] == revision and not current["data"].get("stop_requested"):
                    self.store.update(run_id, revision, status="failed", patch={"error": str(error)[:2000], "pending": None}, kind="safe_stop")
            finally:
                elapsed = time.monotonic() - self.started
                current = self.store.get(run_id)
                if current["revision"] == revision:
                    self.store.update(run_id, revision, patch={"active_seconds": self.active_before + elapsed})
                    self.store.event(run_id, revision, "execution_finished", {"duration_seconds": elapsed})
                    if failure is not None and self.store.get(run_id)["status"] == "failed":
                        self._handoff(run_id, revision, failure)
                    self._write_summary(run_id)
            return self.store.get(run_id)

    def _handoff(self, run_id, revision, error):
        run = self.store.get(run_id)
        data, nodes = run["data"], self.store.nodes(run_id, revision)
        with self.store.connect() as db:
            evidence = [dict(row) for row in db.execute(
                "SELECT id,hash,path,node_id FROM artifacts WHERE run_id=? AND revision=? ORDER BY rowid", (run_id, revision))]
        failed_node = getattr(error, "node", None)
        if failed_node is None:
            failed_node = next((event["payload"]["node"] for event in reversed(self.store.events(run_id))
                                if event["revision"] == revision and event["kind"] in {"node_running", "node_failed"}), "preflight")
        allowed = isinstance(error, RecoverableFailure) and not data.get("recovery")
        handoff = {"run_id": run_id, "revision": revision, "reason": str(error)[:2000],
                   "failure_code": getattr(error, "code", "manual_review_required"), "failed_node": failed_node,
                   "baseline": data.get("baseline"), "candidate": data.get("candidate"), "evidence": evidence,
                   "nodes": {key: {"status": value["status"], "attempts": value["attempts"]} for key, value in nodes.items()},
                   "budgets": {key: data[key] for key in ("calls", "active_seconds", "repairs", "replans")},
                   "budget_limits": data.get("budget_limits", {"calls": self.max_calls, "active_seconds": self.max_seconds}),
                   "permitted_action": "create_replacement" if allowed else "manual_review"}
        reference = self.store.artifact(run_id, revision, "handoff", handoff, [item["id"] for item in evidence])
        self.store.update(run_id, revision, patch={"handoff": reference})
        self.store.event(run_id, revision, "handoff_created", {"hash": reference["hash"], "artifact_id": reference["id"],
                         "permitted_action": handoff["permitted_action"], "failure_code": handoff["failure_code"]})

    def recover(self, run_id, expected_hash, actor, instructions):
        with self.store.lock(run_id):
            run = self.store.get(run_id)
            if run["mode"] != self.provider.mode:
                raise GateFailure("Recovery cannot change execution mode")
            reference = run["data"].get("handoff") or {}
            if reference.get("hash") != expected_hash:
                raise StateConflict("Recovery requires the exact handoff hash")
            handoff = self.store.read_artifact(reference["id"])
            for record in handoff["evidence"]:
                artifact = self.store.read_artifact(record["id"])
                if "evidence_hashes" in artifact:
                    self._verify_evidence(artifact)
            if handoff["candidate"]:
                verify_manifest(Path(handoff["candidate"]["path"]), handoff["candidate"]["manifest"])
            selected = self.store.selected(run["mode"])
            baseline = None
            if selected:
                prior = self.store.get(selected["run_id"])
                if prior["status"] != "succeeded":
                    raise GateFailure("Selected recovery baseline is not accepted")
                baseline = prior["data"]["release"]
                self.verify_release(baseline["id"])
            return self.store.recover(run_id, expected_hash, actor, instructions, baseline,
                                      {"calls": self.max_calls, "active_seconds": self.max_seconds})

    @staticmethod
    def _raise_outcomes(outcomes):
        errors = [item for item in outcomes if isinstance(item, BaseException)]
        if errors:
            # Integrity/policy failure in a parallel branch takes precedence over exhaustion.
            raise next((error for error in errors if not isinstance(error, RecoverableFailure)), errors[0])

    async def _workflow(self, run_id, revision):
        normalization_context = self._context(run_id)
        run = self.store.get(run_id)
        if run["data"].get("normalization_contract"):
            normalization_context["contract"] = run["data"]["normalization_contract"]
        requirement, req_ref = await self._invoke(run_id, revision, "normalize", "normalize", Requirement, normalization_context)
        run = self.store.get(run_id)
        unresolved_aliases = run["scenario"] == "ambiguous" and requirement.alias_min is None
        if requirement.questions or unresolved_aliases:
            self.store.update(run_id, revision, status="waiting_for_input", patch={"questions": requirement.questions or ["Clarify the desired sharing behavior and alias rules."]})
            return
        if requirement.alias_min is not None:
            self.store.update(run_id, revision, patch={"alias_min": requirement.alias_min, "alias_max": requirement.alias_max,
                              "normalization_contract": normalization_context["contract"]}, kind="requirement_constraints_parsed")
        base = self._baseline(run_id, revision)
        context = {**self._context(run_id, snapshot_files(Path(base["path"]))), "normalized": requirement.model_dump(exclude_none=True)}
        plan, plan_ref = await self._invoke(run_id, revision, "plan", "planner", Plan, context, ["inspect"], [req_ref["id"]])
        existing_nodes = self.store.nodes(run_id, revision)
        for task in plan.tasks:
            if task.id not in existing_nodes:
                self.store.node(run_id, revision, task.id, "pending", task.dependencies or ["plan"])
        run = self.store.get(run_id)
        if (run["scenario"] != "greenfield" or run["data"]["baseline"]) and run["data"].get("approved_plan") != plan_ref["hash"]:
            self._gate(run_id, revision, "plan", plan_ref["hash"], plan_ref["id"], "Review the changed API/acceptance scope or additive schema migration.")
            return
        await self._tasks(run_id, revision, plan, plan_ref, base)
        candidate = self.store.get(run_id)["data"].get("candidate")
        if candidate is None:
            candidate = self._assemble(run_id, revision, plan, base)
        while True:
            run = self.store.get(run_id)
            attempt = run["data"]["repairs"]
            verify_manifest(Path(candidate["path"]), candidate["manifest"])
            validation_node, review_node = f"validate_{attempt}", f"review_{attempt}"
            context = {**self._context(run_id, snapshot_files(Path(candidate["path"]))), "plan": plan.model_dump(), "baseline_files": snapshot_files(Path(base["path"]))}
            results = await asyncio.gather(
                self._validate(run_id, revision, validation_node, candidate),
                self._invoke(run_id, revision, review_node, "reviewer", Review, context, ["assemble"], [plan_ref["id"]]),
                return_exceptions=True,
            )
            self._raise_outcomes(results)
            (validation, validation_ref), (review, review_ref) = results
            blocking = [f.model_dump() for f in review.findings if f.severity == "blocking"]
            if validation["passed"] and not blocking:
                if attempt:
                    self.store.event(run_id, revision, "recovery_completed", {"attempt": attempt, "candidate_hash": candidate["manifest"]["hash"]})
                await self._finalize(run_id, revision, candidate, validation, validation_ref, review_ref, plan_ref)
                return
            self.store.event(run_id, revision, "validation_failed", {"attempt": attempt, "summary": validation["summary"], "findings": blocking})
            if attempt >= 2:
                if not validation["passed"] and (not validation.get("protected_test_ids") or not validation.get("evidence")
                        or validation.get("diagnostics") or validation.get("stopped") or validation.get("termination")):
                    raise GateFailure("Repair budget exhausted without verified validation evidence; manual review is required")
                raise RecoverableFailure("Two repair iterations exhausted; retain evidence and request human intervention",
                                         "repair_budget_exhausted", "validation_join")
            repairs = attempt + 1
            repair_context = {**context, "task": "Repair implementation only; tests are immutable.", "failure": validation, "blocking_findings": blocking}
            changes, ref = await self._invoke(run_id, revision, f"repair_{repairs}", "builder", ChangeSet, repair_context,
                                             [validation_node, review_node], [validation_ref["id"], review_ref["id"]])
            candidate = self._apply(run_id, candidate, changes, "builder")
            self.store.update(run_id, revision, patch={"repairs": repairs, "candidate": candidate})
            self.store.event(run_id, revision, "repair_applied", {"attempt": repairs, "artifact": ref["id"]})

    async def _tasks(self, run_id, revision, plan, plan_ref, base):
        by_id = {task.id: task for task in plan.tasks}
        while True:
            nodes = self.store.nodes(run_id, revision)
            remaining = [task for task in plan.tasks if nodes.get(task.id, {}).get("status") != "passed"]
            if not remaining:
                return
            ready = [task for task in remaining if all(nodes.get(dep, {}).get("status") == "passed" for dep in task.dependencies)]
            if not ready:
                raise GateFailure("No ready tasks; dependency gate blocked")

            async def execute(task):
                source = base
                def depth(task_id):
                    return 1 + max((depth(dep) for dep in by_id[task_id].dependencies), default=0)
                prior_writers = [dep for dep in task.dependencies if by_id[dep].kind == "implementation"]
                if prior_writers:
                    latest = max(prior_writers, key=depth)
                    source = nodes[latest]["result"]["candidate"]
                verify_manifest(Path(source["path"]), source["manifest"])
                context = {**self._context(run_id, snapshot_files(Path(source["path"]))), "task": task.model_dump(), "plan": plan.model_dump()}
                role = "builder" if task.kind == "implementation" else "verifier"
                output, ref = await self._invoke(run_id, revision, "call:" + task.id, role, ChangeSet, context, task.dependencies or ["plan"], [plan_ref["id"]])
                self._check(run_id, revision)
                candidate = self._apply(run_id, source, output, role)
                self.store.node(run_id, revision, task.id, "passed", task.dependencies or ["plan"], {"candidate": candidate, "changes": ref})

            outcomes = await asyncio.gather(*(execute(task) for task in ready[:2]), return_exceptions=True)
            self._raise_outcomes(outcomes)

    def _apply(self, run_id, base, changes, role):
        verify_manifest(Path(base["path"]), base["manifest"])
        destination = self.store.root / run_id / "candidates" / uuid.uuid4().hex
        result = apply_changes(Path(base["path"]), destination, [change.model_dump() for change in changes.changes], role)
        (destination / "generated_tests").mkdir(exist_ok=True)
        return {"path": str(destination), "manifest": result}

    def _assemble(self, run_id, revision, plan, base):
        nodes = self.store.nodes(run_id, revision)
        impl = [task for task in plan.tasks if task.kind == "implementation"]
        referenced = {dep for task in impl for dep in task.dependencies}
        last = next(task for task in impl if task.id not in referenced)
        candidate = nodes[last.id]["result"]["candidate"]
        test_task = next(task for task in plan.tasks if task.kind == "tests")
        test_ref = nodes[test_task.id]["result"]["changes"]
        changes = ChangeSet.model_validate(self.store.read_artifact(test_ref["id"])["output"])
        candidate = self._apply(run_id, candidate, changes, "verifier")
        self.store.node(run_id, revision, "assemble", "passed", [task.id for task in plan.tasks], candidate)
        self.store.update(run_id, revision, patch={"candidate": candidate})
        return candidate

    async def _validate(self, run_id, revision, node_id, candidate):
        run = self._check(run_id, revision)
        verify_manifest(Path(candidate["path"]), candidate["manifest"])
        input_hash = digest({"candidate_hash": candidate["manifest"]["hash"],
                             "scenario": run["scenario"], "alias_min": run["data"]["alias_min"],
                             "alias_max": run["data"]["alias_max"]})
        prior = self.store.nodes(run_id, revision).get(node_id)
        if prior and prior["status"] == "passed":
            record = prior["result"]
            cached = self.store.read_artifact(record["id"])
            if cached.get("input_hash") != input_hash or cached.get("candidate_hash") != candidate["manifest"]["hash"]:
                raise GateFailure("Cached validation inputs changed; create a new requirement revision")
            self._verify_evidence(cached)
            self._check(run_id, revision)
            return cached, record
        self.store.node(run_id, revision, node_id, "running", ["assemble"])
        path = self.store.root / run_id / "validation" / uuid.uuid4().hex

        def stopped():
            try:
                self._check(run_id, revision)
                return False
            except (GateFailure, StateConflict):
                return True

        result = await asyncio.to_thread(self.runner.validate, Path(candidate["path"]), path, run["scenario"], run["data"]["alias_min"], run["data"]["alias_max"], stopped)
        self._check(run_id, revision)
        verify_manifest(Path(candidate["path"]), candidate["manifest"])
        evidence_files = self._capture_evidence(path, result)
        result.update(input_hash=input_hash, candidate_hash=candidate["manifest"]["hash"], evidence_dir=str(path), evidence_hashes=evidence_files)
        record = self.store.artifact(run_id, revision, node_id, result)
        # A completed validation node may report failed tests; the join decides success.
        self.store.node(run_id, revision, node_id, "passed", ["assemble"], record)
        return result, record

    @staticmethod
    def _capture_evidence(directory, result):
        if directory.is_symlink() or directory.resolve() != directory:
            raise GateFailure("Validation evidence directory must not contain symlinks")
        exported = result.get("artifacts", {})
        if not isinstance(exported, dict):
            raise GateFailure("Validation artifacts must be a mapping")
        names = {"report.json"} | set(exported)
        names.update(name for name in ("stdout.log", "stderr.log") if (directory / name).exists() or (directory / name).is_symlink())
        hashes = {}
        for name in names:
            if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name or "\\" in name or "\x00" in name:
                raise GateFailure("Validation evidence must use direct child filenames")
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise GateFailure("Validation evidence must be a direct regular file")
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if name in exported:
                record = exported[name]
                if not isinstance(record, dict) or Path(record.get("path", "")).absolute() != path or record.get("sha256") != actual_hash:
                    raise GateFailure("Exported validation evidence changed or points outside its directory")
            hashes[name] = actual_hash
        return dict(sorted(hashes.items()))

    async def _finalize(self, run_id, revision, candidate, validation, validation_ref, review_ref, plan_ref):
        self.store.node(run_id, revision, "validation_join", "passed", ["validate_" + str(self.store.get(run_id)["data"]["repairs"]), "review_" + str(self.store.get(run_id)["data"]["repairs"])])
        context = {**self._context(run_id), "validation": validation, "plan": self.store.read_artifact(plan_ref["id"])["output"]}
        docs, docs_ref = await self._invoke(run_id, revision, "docs", "documenter", Documentation, context,
                                          ["validation_join"], [validation_ref["id"], review_ref["id"], plan_ref["id"]])
        release = {"mode": self.provider.mode, "revision": revision, "candidate": candidate,
                   "validation": validation_ref, "review": review_ref, "documentation": docs_ref,
                   "plan": plan_ref,
                   "notice": "Bounded local rule-based generation validated in Docker; no model inference"}
        release_ref = self.store.artifact(run_id, revision, "release", release,
                                          [validation_ref["id"], review_ref["id"], docs_ref["id"], plan_ref["id"]])
        self.store.update(run_id, revision, patch={"release": release_ref})
        self._gate(run_id, revision, "accept", release_ref["hash"], release_ref["id"], "Review the exact candidate, actual test evidence, limitations and release manifest.")

    def verify_release(self, artifact_id):
        release = self.store.read_artifact(artifact_id)
        if release["mode"] != self.provider.mode:
            raise GateFailure("Release execution mode does not match this engine")
        verify_manifest(Path(release["candidate"]["path"]), release["candidate"]["manifest"])
        for key in ("validation", "review", "documentation", "plan"):
            self.store.read_artifact(release[key]["id"])
        evidence = self.store.read_artifact(release["validation"]["id"])
        if not evidence["passed"] or evidence["candidate_hash"] != release["candidate"]["manifest"]["hash"]:
            raise GateFailure("Release does not have matching successful validation")
        if any(f["severity"] == "blocking" for f in self.store.read_artifact(release["review"]["id"])["output"]["findings"]):
            raise GateFailure("Release has unresolved blocking findings")
        self._verify_evidence(evidence)
        return release

    @staticmethod
    def _verify_evidence(evidence):
        for relative, expected in evidence["evidence_hashes"].items():
            path = Path(evidence["evidence_dir"]) / relative
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise GateFailure("Validation evidence changed after execution")

    def approve(self, run_id, action, expected_hash, actor, decision="approve"):
        with self.store.lock(run_id):
            run = self.store.get(run_id)
            if run["mode"] != self.provider.mode:
                raise GateFailure("Approval cannot change execution mode")
            pending = run["data"].get("pending")
            if not pending:
                raise GateFailure("No current approval gate")
            if decision == "approve":
                self.store.read_artifact(pending["artifact_id"])
                if action == "accept":
                    self.verify_release(pending["artifact_id"])
            self.store.approve(run_id, action, expected_hash, actor, decision)
            self._write_summary(run_id)

    def rollback(self, target_run, expected_current, actor):
        if not actor.strip():
            raise ValueError("An actor is required")
        with self.store.lock(target_run):
            run = self.store.get(target_run)
            self.verify_release(run["data"]["release"]["id"])
            self.store.rollback(target_run, expected_current, actor)

    def _write_summary(self, run_id):
        run = self.store.get(run_id)
        data = run["data"]
        payload = {"id": run_id, "mode": run["mode"], "scenario": run["scenario"], "status": run["status"],
                   "revision": run["revision"], "calls": data["calls"], "active_seconds": data["active_seconds"],
                   "repairs": data["repairs"], "pending": data.get("pending"), "questions": data.get("questions", []),
                   "error": data.get("error"), "release": data.get("release"),
                   "handoff": data.get("handoff"), "recovery": data.get("recovery"), "recovery_child": data.get("recovery_child"),
                   "limitations": ["Local rules support a bounded vocabulary and known source structure; no model inference."]}
        self.store.artifact(run_id, run["revision"], "summary", payload)
