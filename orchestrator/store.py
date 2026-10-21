"""Local transactional state and append-only execution evidence."""

import contextlib
import fcntl
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class StateConflict(RuntimeError):
    pass


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "control.sqlite3"
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, mode TEXT NOT NULL, scenario TEXT NOT NULL,
                revision INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS nodes (
                run_id TEXT NOT NULL, revision INTEGER NOT NULL, id TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                dependencies TEXT NOT NULL, result TEXT,
                PRIMARY KEY(run_id, revision, id));
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                revision INTEGER NOT NULL, timestamp TEXT NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
                node_id TEXT NOT NULL, hash TEXT NOT NULL, path TEXT NOT NULL,
                inputs TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
                action TEXT NOT NULL, hash TEXT NOT NULL, actor TEXT NOT NULL,
                decision TEXT NOT NULL, timestamp TEXT NOT NULL, valid INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS accepted (
                mode TEXT PRIMARY KEY, run_id TEXT NOT NULL, hash TEXT NOT NULL,
                artifact_id TEXT NOT NULL);
            """)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def lock(self, run_id):
        self.get(run_id)
        path = self.root / run_id / "process.lock"
        with path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise StateConflict("This run already has an active process") from error
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def create(self, scenario, mode, requirement, baseline=None, inject_fault=False, budget_limits=None):
        run_id = uuid.uuid4().hex[:12]
        stamp = now()
        (self.root / run_id / "artifacts").mkdir(parents=True)
        data = {
            "requirement": requirement, "answers": "", "baseline": baseline,
            "calls": 0, "active_seconds": 0.0, "repairs": 0, "replans": 0,
            "alias_min": 4, "alias_max": 32, "inject_fault": inject_fault,
            "fault_injected": False, "stop_requested": False,
            "budget_limits": budget_limits or {"calls": 20, "active_seconds": 1800},
        }
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            selected = db.execute("SELECT run_id,hash,artifact_id FROM accepted WHERE mode=?", (mode,)).fetchone()
            data["expected_current"] = selected["hash"] if selected else None
            if baseline and (not selected or baseline.get("hash") != selected["hash"] or baseline.get("id") != selected["artifact_id"]):
                raise StateConflict("Baseline is no longer selected; start a new run from the current accepted baseline")
            if baseline and mode == "local" and scenario != "ambiguous":
                baseline_data = json.loads(db.execute("SELECT data FROM runs WHERE id=?", (selected["run_id"],)).fetchone()["data"])
                data.update({key: baseline_data[key] for key in ("alias_min", "alias_max")})
            db.execute("INSERT INTO runs VALUES (?, ?, ?, 1, 'running', ?, ?, ?)",
                       (run_id, mode, scenario, stamp, stamp, json.dumps(data)))
            self._event(db, run_id, 1, "run_created", {"mode": mode, "scenario": scenario})
        return self.get(run_id)

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown run")
        result = dict(row)
        result["data"] = json.loads(result["data"])
        return result

    def list_runs(self):
        with self.connect() as db:
            return [self.get(row[0]) for row in db.execute("SELECT id FROM runs ORDER BY created_at")]

    def _event(self, db, run_id, revision, kind, payload):
        db.execute("INSERT INTO events(run_id,revision,timestamp,kind,payload) VALUES(?,?,?,?,?)",
                   (run_id, revision, now(), kind, json.dumps(payload, sort_keys=True)))

    def event(self, run_id, revision, kind, payload):
        with self.connect() as db:
            self._event(db, run_id, revision, kind, payload)

    def update(self, run_id, revision, *, status=None, patch=None, kind="state_updated"):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            data = json.loads(row["data"])
            if row["revision"] != revision:
                raise StateConflict("Run revision changed; discard stale work")
            if data.get("stop_requested") and status not in (None, "stopped"):
                raise StateConflict("Run was stopped; discard late result")
            if data.get("handoff") and status not in (None, "failed"):
                raise StateConflict("Handed-off runs are terminal; use an eligible recovery handoff")
            data.update(patch or {})
            db.execute("UPDATE runs SET status=?, updated_at=?, data=? WHERE id=?",
                       (status or row["status"], now(), json.dumps(data), run_id))
            self._event(db, run_id, revision, kind, {"status": status or row["status"], "keys": sorted(patch or {})})

    def nodes(self, run_id, revision=None):
        revision = revision or self.get(run_id)["revision"]
        with self.connect() as db:
            rows = db.execute("SELECT * FROM nodes WHERE run_id=? AND revision=?", (run_id, revision)).fetchall()
        return {row["id"]: {**dict(row), "dependencies": json.loads(row["dependencies"]),
                            "result": json.loads(row["result"]) if row["result"] else None} for row in rows}

    def node(self, run_id, revision, node_id, status, dependencies=(), result=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT revision,status,data FROM runs WHERE id=?", (run_id,)).fetchone()
            data = json.loads(run["data"])
            if run["revision"] != revision or run["status"] == "failed" or data.get("stop_requested") or data.get("handoff"):
                raise StateConflict("Node result is no longer eligible")
            db.execute("""INSERT INTO nodes VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(run_id,revision,id) DO UPDATE SET status=excluded.status,
                attempts=nodes.attempts + excluded.attempts,
                dependencies=excluded.dependencies,result=excluded.result""",
                       (run_id, revision, node_id, status, int(status == "running"), json.dumps(list(dependencies)),
                        json.dumps(result) if result is not None else None))
            self._event(db, run_id, revision, "node_" + status, {"node": node_id, "dependencies": list(dependencies)})

    def artifact(self, run_id, revision, node_id, value, inputs=()):
        artifact_id = uuid.uuid4().hex
        content = json.dumps(value, sort_keys=True, indent=2) + "\n"
        path = self.root / run_id / "artifacts" / (artifact_id + ".json")
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
        record = {"id": artifact_id, "hash": hashlib.sha256(content.encode()).hexdigest(), "path": str(path)}
        with self.connect() as db:
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
                       (artifact_id, run_id, revision, node_id, record["hash"], str(path), json.dumps(list(inputs))))
        return record

    def read_artifact(self, artifact_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown artifact")
        path = Path(row["path"])
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise StateConflict("Artifact path changed")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != row["hash"]:
            raise StateConflict("Artifact integrity check failed")
        return json.loads(content)

    def events(self, run_id):
        with self.connect() as db:
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in
                    db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))]

    def selected(self, mode):
        with self.connect() as db:
            row = db.execute("SELECT * FROM accepted WHERE mode=?", (mode,)).fetchone()
            return dict(row) if row else None

    def recover(self, run_id, expected_hash, actor, instructions, baseline, budget_limits):
        """Atomically authorize one replacement and bind both sides of its lineage."""
        if not actor.strip() or not instructions.strip():
            raise ValueError("Recovery requires an actor and explicit recovery instructions")
        child_id, stamp = uuid.uuid4().hex[:12], now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown run")
            data = json.loads(row["data"])
            reference = data.get("handoff") or {}
            if row["status"] != "failed" or data.get("recovery") or data.get("recovery_child"):
                raise StateConflict("Recovery requires an unused failed parent; replacement runs cannot recover again")
            record = db.execute("SELECT * FROM artifacts WHERE id=?", (reference.get("id"),)).fetchone()
            if (not record or record["run_id"] != run_id or record["revision"] != row["revision"]
                    or record["node_id"] != "handoff" or reference.get("hash") != expected_hash
                    or record["hash"] != expected_hash):
                raise StateConflict("Recovery handoff hash or ownership changed")
            handoff = self.read_artifact(record["id"])
            if (handoff.get("run_id") != run_id or handoff.get("revision") != row["revision"]
                    or handoff.get("permitted_action") != "create_replacement"):
                raise StateConflict("This handoff requires manual review; replacement is not permitted")
            selected = db.execute("SELECT * FROM accepted WHERE mode=?", (row["mode"],)).fetchone()
            if selected:
                if not baseline or (baseline.get("hash"), baseline.get("id")) != (selected["hash"], selected["artifact_id"]):
                    raise StateConflict("Selected baseline changed before recovery; review and retry")
            elif baseline or row["scenario"] != "greenfield":
                raise StateConflict("Recovery requires the current accepted baseline")
            recovery = {"parent_run": run_id, "parent_revision": row["revision"],
                        "handoff_id": record["id"], "handoff_hash": expected_hash,
                        "actor": actor, "instructions": instructions}
            child = {key: data[key] for key in ("requirement", "answers", "alias_min", "alias_max")}
            if selected and row["mode"] == "local" and row["scenario"] != "ambiguous":
                baseline_data = json.loads(db.execute("SELECT data FROM runs WHERE id=?", (selected["run_id"],)).fetchone()["data"])
                child.update({key: baseline_data[key] for key in ("alias_min", "alias_max")})
            child.update(baseline=baseline, expected_current=selected["hash"] if selected else None,
                         calls=0, active_seconds=0.0, repairs=0, replans=0, inject_fault=False,
                         fault_injected=False, stop_requested=False, recovery=recovery, budget_limits=budget_limits)
            (self.root / child_id / "artifacts").mkdir(parents=True)
            db.execute("INSERT INTO runs VALUES (?, ?, ?, 1, 'running', ?, ?, ?)",
                       (child_id, row["mode"], row["scenario"], stamp, stamp, json.dumps(child)))
            data["recovery_child"] = child_id
            db.execute("UPDATE runs SET updated_at=?,data=? WHERE id=?", (stamp, json.dumps(data), run_id))
            db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,1)",
                       (uuid.uuid4().hex, run_id, row["revision"], "recover", expected_hash, actor, "approve", stamp))
            self._event(db, run_id, row["revision"], "human_decision", {
                "action": "recover", "hash": expected_hash, "actor": actor, "decision": "approve"})
            self._event(db, run_id, row["revision"], "recovery_created", {"child_run": child_id, **recovery})
            self._event(db, child_id, 1, "run_created", {"mode": row["mode"], "scenario": row["scenario"], "recovery": recovery})
        return self.get(child_id)

    def approve(self, run_id, action, expected_hash, actor, decision="approve"):
        if not actor.strip() or decision not in ("approve", "reject"):
            raise ValueError("An actor and valid decision are required")
        conflict = None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            data = json.loads(row["data"])
            pending = data.get("pending", {})
            if row["status"] != "waiting_for_approval" or pending.get("action") != action or pending.get("hash") != expected_hash:
                raise StateConflict("Approval does not match the current gate")
            if action == "accept" and decision == "approve":
                selected = db.execute("SELECT hash FROM accepted WHERE mode=?", (row["mode"],)).fetchone()
                actual_current = selected[0] if selected else None
                if actual_current != data.get("expected_current") or pending.get("expected_current") != data.get("expected_current"):
                    conflict = "Accepted baseline changed; run stopped. Start a new run from the current accepted baseline"
                    data.update(pending=None, stop_requested=True, error=conflict)
                    db.execute("UPDATE approvals SET valid=0 WHERE run_id=?", (run_id,))
                    db.execute("UPDATE runs SET status='stopped',updated_at=?,data=? WHERE id=?", (now(), json.dumps(data), run_id))
                    self._event(db, run_id, row["revision"], "acceptance_conflict", {
                        "actor": actor, "hash": expected_hash,
                        "expected_current": data.get("expected_current"), "actual_current": actual_current,
                    })
                else:
                    db.execute("INSERT INTO accepted VALUES(?,?,?,?) ON CONFLICT(mode) DO UPDATE SET run_id=excluded.run_id,hash=excluded.hash,artifact_id=excluded.artifact_id",
                               (row["mode"], run_id, expected_hash, pending["artifact_id"]))
            if conflict is None:
                db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,1)",
                           (uuid.uuid4().hex, run_id, row["revision"], action, expected_hash, actor, decision, now()))
                data["pending"] = None
                if decision == "reject":
                    data["stop_requested"] = True
                if action == "plan" and decision == "approve":
                    data["approved_plan"] = expected_hash
                status = "stopped" if decision == "reject" else ("succeeded" if action == "accept" else "running")
                db.execute("UPDATE runs SET status=?,updated_at=?,data=? WHERE id=?", (status, now(), json.dumps(data), run_id))
                self._event(db, run_id, row["revision"], "human_decision", {"action": action, "hash": expected_hash, "actor": actor, "decision": decision})
        if conflict:
            raise StateConflict(conflict)

    def revise(self, run_id, answer, *, alias_min=None, alias_max=None):
        """Revise state; callers must hold self.lock(run_id) if execution may overlap.

        The database transaction alone does not exclude active model or runner work.
        """
        if not answer.strip():
            raise ValueError("An explicit human answer or requirement change is required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            data = json.loads(row["data"])
            if row["status"] in ("succeeded", "stopped", "failed") or data.get("handoff"):
                raise StateConflict("Create a new run from the accepted baseline")
            if data["replans"] >= 2:
                raise StateConflict("Clarification/replan budget exhausted; start a new run")
            revision = row["revision"] + 1
            retained = {key: data[key] for key in ("requirement", "baseline", "calls", "active_seconds", "replans", "alias_min", "alias_max", "inject_fault", "fault_injected")}
            retained["expected_current"] = data.get("expected_current")
            for key in ("recovery", "budget_limits"):
                if key in data:
                    retained[key] = data[key]
            retained.update(answers=answer, repairs=0, stop_requested=False, replans=data["replans"] + 1)
            retained["alias_min"] = alias_min if alias_min is not None else data["alias_min"]
            retained["alias_max"] = alias_max if alias_max is not None else data["alias_max"]
            if not 4 <= retained["alias_min"] <= retained["alias_max"] <= 32:
                raise ValueError("Alias bounds must satisfy 4 <= min <= max <= 32")
            db.execute("UPDATE nodes SET status='stale' WHERE run_id=? AND revision=?", (run_id, row["revision"]))
            db.execute("UPDATE approvals SET valid=0 WHERE run_id=?", (run_id,))
            db.execute("UPDATE runs SET revision=?,status='running',updated_at=?,data=? WHERE id=?", (revision, now(), json.dumps(retained), run_id))
            self._event(db, run_id, revision, "requirement_revised", {"answer": answer, "previous_revision": row["revision"]})

    def stop(self, run_id):
        run = self.get(run_id)
        if run["status"] in ("succeeded", "stopped", "failed") or run["data"].get("handoff"):
            raise StateConflict("Run is already terminal")
        self.update(run_id, run["revision"], status="stopped", patch={"stop_requested": True, "pending": None}, kind="stop_requested")

    def rollback(self, target_run, expected_current, actor):
        target = self.get(target_run)
        release = target["data"].get("release")
        if not release or target["status"] != "succeeded":
            raise StateConflict("Rollback target must be an accepted candidate")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM accepted WHERE mode=?", (target["mode"],)).fetchone()
            if not current or current["hash"] != expected_current or current["hash"] == release["hash"]:
                raise StateConflict("Rollback needs a matching, different current candidate")
            db.execute("UPDATE accepted SET run_id=?,hash=?,artifact_id=? WHERE mode=?", (target_run, release["hash"], release["id"], target["mode"]))
            current_revision = db.execute("SELECT revision FROM runs WHERE id=?", (current["run_id"],)).fetchone()[0]
            self._event(db, current["run_id"], current_revision, "rollback", {"actor": actor, "from": expected_current, "to": release["hash"], "target_run": target_run})
