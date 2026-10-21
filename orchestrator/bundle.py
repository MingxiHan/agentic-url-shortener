"""Portable, unsigned historical evidence bundles; verification never executes code."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .artifacts import _open_directory, manifest as candidate_manifest, snapshot_files
from .runner import DockerRunner, PROJECT_ROOT, _trusted_fingerprint


FORMAT = "agentic-shortener-evidence-v1"
NOTICE = "Historical evidence only; verification does not rerun tests or execute candidate code. Local integrity is not signed provenance."
MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES = 4000, 8 * 1024 * 1024, 128 * 1024 * 1024
HEX = re.compile(r"^[a-f0-9]{64}$")
IDENTIFIER = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


class BundleError(ValueError):
    """Evidence is incomplete, inconsistent, unsafe, or outside bundle limits."""


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise BundleError("Bundle paths must be relative POSIX paths")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise BundleError("Control characters in bundle path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts) or PurePosixPath(value).is_absolute():
        raise BundleError("Absolute paths and traversal are forbidden")
    return value


def _root(path: Path) -> Path:
    absolute = Path(path).absolute()
    if absolute.is_symlink() or absolute.resolve() != absolute or not absolute.is_dir():
        raise BundleError("Bundle/source root must be a real directory without symlink parents")
    return absolute


def _read(root: Path, relative: str) -> bytes:
    path = root
    for part in _relative(relative).split("/"):
        path = path / part
        if path.is_symlink():
            raise BundleError("Symlinks are forbidden in bundle inputs")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
            raise BundleError("Evidence must be a bounded regular file with one hard link")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(descriptor)
        if len(raw) > MAX_FILE_BYTES or (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise BundleError("Evidence was oversized or changed while being read")
        return raw
    finally:
        os.close(descriptor)


def _source(root: Path, path: str) -> bytes:
    try:
        relative = Path(path).relative_to(root).as_posix()
    except ValueError as error:
        raise BundleError("Evidence source escapes run storage") from error
    return _read(root, relative)


def _excluded(relative: str) -> bool:
    path = PurePosixPath(_relative(relative))
    return path.parts[0] == "runtime" or path.suffix.lower() in {".db", ".sqlite", ".sqlite3"} or path.name.endswith(("-wal", "-shm"))


def _json(raw: bytes):
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise BundleError("Invalid evidence JSON") from error


def _encode(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _smoke_required_files(report) -> set[str]:
    """Logs may be absent only when the runner explicitly never dispatched work."""
    if not isinstance(report, dict) or type(report.get("passed")) is not bool:
        raise BundleError("Rollback smoke report is malformed")
    artifacts = report.get("artifacts", {})
    if not isinstance(artifacts, dict):
        raise BundleError("Rollback smoke artifacts must be a mapping")
    required = {"report.json"} | {name for name in artifacts if not _excluded(name)}
    summary = report.get("summary", "")
    stopped_before_start = report.get("stopped") is True and (
        report.get("container_retained") is False and summary == "Validation stopped before container creation"
    )
    creation_failed = (report.get("container_retained") is False and isinstance(summary, str)
                       and summary.startswith("Docker container creation failed:"))
    never_executed = (
        report["passed"] is False and report.get("exit_code") == -1
        and type(report.get("tests")) is int and report["tests"] == 0
        and not artifacts and not report.get("evidence")
        and not any(key in report for key in ("container_state", "cli_exit_code", "termination"))
        and (stopped_before_start or creation_failed)
    )
    if not never_executed:
        required.update({"stdout.log", "stderr.log"})
    return required


def export_bundle(store, run_ids: list[str], destination: Path) -> dict:
    """Copy selected runs with accepted baselines and recorded recovery lineage."""
    if not run_ids or len(run_ids) > 100 or any(not isinstance(item, str) for item in run_ids):
        raise BundleError("Select between one and 100 run IDs")
    source_root = _root(store.root)
    with store.connect() as db:
        db.execute("BEGIN")
        tables = {table: [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
                  for table in ("runs", "nodes", "events", "artifacts", "approvals", "accepted")}
    for table, fields in {"runs": ("data",), "nodes": ("dependencies", "result"),
                          "events": ("payload",), "artifacts": ("inputs",)}.items():
        for row in tables[table]:
            for field in fields:
                row[field] = json.loads(row[field]) if row[field] is not None else None
    all_runs = {run["id"]: run for run in tables["runs"]}
    all_artifacts = {record["id"]: record for record in tables["artifacts"]}
    included, queue = set(), list(run_ids)
    while queue:
        run_id = queue.pop()
        if run_id in included:
            continue
        if run_id not in all_runs or not IDENTIFIER.fullmatch(run_id):
            raise BundleError("Unknown or invalid run ID")
        if all_runs[run_id]["mode"] != "local":
            raise BundleError("Only local execution evidence is supported")
        included.add(run_id)
        if len(included) > 100:
            raise BundleError("Too many baseline dependencies")
        baseline = all_runs[run_id]["data"].get("baseline")
        if baseline:
            record = all_artifacts.get(baseline.get("id"))
            if not record:
                raise BundleError("Missing baseline artifact")
            queue.append(record["run_id"])
        recovery = all_runs[run_id]["data"].get("recovery")
        if recovery:
            queue.append(recovery["parent_run"])
        recovery_child = all_runs[run_id]["data"].get("recovery_child")
        if recovery_child:
            queue.append(recovery_child)
    metadata = {table: [row for row in rows if row["id"] in included] if table == "runs"
                else [row for row in rows if row["run_id"] in included] for table, rows in tables.items()}
    files: dict[str, bytes] = {}
    size = 0

    def add(relative: str, raw: bytes):
        nonlocal size
        relative = _relative(relative)
        if relative in files:
            if files[relative] != raw:
                raise BundleError("Conflicting bundle paths")
            return
        size += len(raw)
        if len(files) >= MAX_FILES or len(raw) > MAX_FILE_BYTES or size > MAX_TOTAL_BYTES:
            raise BundleError("Bundle exceeds bounded file/size limits")
        files[relative] = raw

    index = {"metadata": "metadata.json", "artifacts": {}, "candidates": {}, "evidence": {},
             "acceptance": "trusted_acceptance", "prompts": {}, "rules": {}}
    values = {}
    for record in metadata["artifacts"]:
        if not IDENTIFIER.fullmatch(record["id"]):
            raise BundleError("Invalid artifact ID")
        raw = _source(source_root, record["path"])
        if _hash(raw) != record["hash"]:
            raise BundleError("Original artifact hash mismatch")
        relative = f"artifacts/{record['id']}.json"
        add(relative, raw)
        index["artifacts"][record["id"]] = relative
        values[record["id"]] = _json(raw)
        prompt = values[record["id"]].get("prompt", {}) if isinstance(values[record["id"]], dict) else {}
        # Earlier local records included role templates; current local dispatches do not.
        if "role_template" in prompt:
            template = _relative(prompt["role_template"])
            if not template.startswith("prompts/") or not template.endswith(".txt"):
                raise BundleError("Unsupported prompt provenance path")
            source = _read(PROJECT_ROOT, template)
            if _hash(source) != prompt["role_template_hash"]:
                raise BundleError("Historical prompt template is no longer available")
            exported = f"provenance/{template}"
            add(exported, source)
            index["prompts"][template] = exported
        rule_sources = values[record["id"]].get("metadata", {}).get("rule_sources", {})
        for name, expected_hash in rule_sources.items():
            name = _relative(name)
            if name not in {"orchestrator/local_provider.py", "orchestrator/domain.py"} and not (name.startswith("orchestrator/templates/") and name.endswith(".txt")):
                raise BundleError("Unsupported rule provenance path")
            source = _read(PROJECT_ROOT, name)
            if _hash(source) != expected_hash:
                raise BundleError("Historical local rule source is no longer available")
            exported = f"provenance/{name}"
            add(exported, source)
            index["rules"][name] = exported

    def candidate(reference):
        if reference is None:
            return
        expected = reference["manifest"]
        hash_value = expected["hash"]
        if not HEX.fullmatch(hash_value):
            raise BundleError("Invalid candidate hash")
        original = Path(reference["path"])
        if not original.is_relative_to(source_root):
            raise BundleError("Candidate source escapes run storage")
        if candidate_manifest(original) != expected:
            raise BundleError("Original candidate hash mismatch")
        contents = snapshot_files(original)
        directory = f"candidates/{hash_value}"
        index["candidates"][hash_value] = directory
        for relative, content in contents.items():
            add(f"{directory}/{relative}", content.encode())

    for run in metadata["runs"]:
        for key in ("base", "candidate"):
            candidate(run["data"].get(key))
    for node in metadata["nodes"]:
        result = node["result"]
        if isinstance(result, dict):
            if "manifest" in result and "path" in result:
                candidate(result)
            candidate(result.get("candidate"))
    for record in metadata["artifacts"]:
        value = values[record["id"]]
        if record["node_id"] == "release":
            candidate(value["candidate"])
        if isinstance(value, dict) and "evidence_dir" in value and "evidence_hashes" in value:
            mapping, excluded = {}, []
            for relative, expected in value["evidence_hashes"].items():
                if _excluded(relative):
                    excluded.append(relative)
                    continue
                raw = _source(source_root, str(Path(value["evidence_dir"]) / relative))
                if _hash(raw) != expected:
                    raise BundleError("Original validation evidence hash mismatch")
                exported = f"evidence/{record['id']}/{relative}"
                add(exported, raw)
                mapping[relative] = exported
            index["evidence"][record["id"]] = {"files": mapping, "excluded": sorted(excluded)}
    for event in metadata["events"]:
        if event["kind"] == "rollback_smoke" and isinstance(event["payload"].get("report"), str):
            report_path = Path(event["payload"]["report"])
            key = f"event-{event['seq']}"
            mapping = {}
            try:
                report = _json(_source(source_root, str(report_path)))
            except OSError as error:
                raise BundleError("Rollback smoke report is missing or unreadable") from error
            required = _smoke_required_files(report)
            for name in sorted(required | {"stdout.log", "stderr.log"}):
                relative = f"evidence/{key}/{_relative(name)}"
                try:
                    raw = _source(source_root, str(report_path.parent / name))
                except FileNotFoundError as error:
                    if name not in required:
                        continue
                    raise BundleError(f"Rollback smoke evidence is missing required file: {name}") from error
                except OSError as error:
                    raise BundleError(f"Rollback smoke evidence is unreadable: {name}") from error
                add(relative, raw)
                mapping[name] = relative
            index["evidence"][key] = {"files": mapping, "excluded": []}
    acceptance = _root(PROJECT_ROOT / "acceptance")
    for path in sorted(acceptance.rglob("*")):
        if path.suffix in {".py", ".json"}:
            relative = path.relative_to(acceptance).as_posix()
            add(f"trusted_acceptance/{relative}", _read(acceptance, relative))
    add("metadata.json", _encode(metadata))
    document = {"format": FORMAT, "notice": NOTICE, "requested_runs": list(dict.fromkeys(run_ids)), "index": index,
                "files": {name: {"sha256": _hash(raw), "size": len(raw)} for name, raw in sorted(files.items())}}
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise BundleError("Bundle destination must be a new directory")
    descriptor = None
    try:
        descriptor = _open_directory(destination.parent, create=True)
        os.mkdir(destination.name, mode=0o700, dir_fd=descriptor)
    except (OSError, ValueError) as error:
        raise BundleError("Cannot create bundle destination without following symlinks") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    for relative, raw in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(raw)
    with (destination / "manifest.json").open("xb") as stream:
        stream.write(_encode(document))
    return verify_bundle(destination)


def verify_bundle(path: Path) -> dict:
    """Verify local integrity and historical acceptance bindings using relative paths only."""
    try:
        return _verify_bundle(path)
    except BundleError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        raise BundleError(f"Invalid or incomplete evidence bundle: {error}") from error


def _verify_bundle(path: Path) -> dict:
    root = _root(path)
    document = _json(_read(root, "manifest.json"))
    if document.get("format") != FORMAT or not isinstance(document.get("files"), dict):
        raise BundleError("Unknown bundle format")
    inventory = document["files"]
    if len(inventory) > MAX_FILES:
        raise BundleError("Bundle has too many files")
    observed = set()
    allowed_dirs = {str(parent) for relative in inventory for parent in PurePosixPath(_relative(relative)).parents if str(parent) != "."}
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            item = Path(directory) / name
            if item.is_symlink():
                raise BundleError("Bundle contains symlinks")
            if name in dirs and item.relative_to(root).as_posix() not in allowed_dirs:
                raise BundleError("Bundle contains an unmanifested directory")
        for name in names:
            observed.add((Path(directory) / name).relative_to(root).as_posix())
            if len(observed) > MAX_FILES + 1:
                raise BundleError("Bundle has too many files")
    if observed != set(inventory) | {"manifest.json"}:
        raise BundleError("Bundle file inventory differs from manifest")
    raw_files, size = {}, 0
    for relative, entry in inventory.items():
        raw = _read(root, relative)
        size += len(raw)
        if size > MAX_TOTAL_BYTES or len(raw) != entry["size"] or _hash(raw) != entry["sha256"]:
            raise BundleError("Bundle file hash or size mismatch")
        raw_files[relative] = raw
    index = document["index"]
    if index["metadata"] != "metadata.json" or index["acceptance"] != "trusted_acceptance":
        raise BundleError("Unexpected metadata or protected-suite index")

    def indexed(relative):
        relative = _relative(relative)
        if relative not in raw_files:
            raise BundleError("Index references an unmanifested file")
        return _json(raw_files[relative])

    metadata = indexed(index["metadata"])
    runs = {run["id"]: run for run in metadata["runs"]}
    records = {record["id"]: record for record in metadata["artifacts"]}
    if not 1 <= len(runs) <= 100 or len(runs) != len(metadata["runs"]) or len(records) != len(metadata["artifacts"]) or set(records) != set(index["artifacts"]):
        raise BundleError("Duplicate or missing run/artifact metadata")
    if not set(document["requested_runs"]).issubset(runs):
        raise BundleError("Requested runs are missing")
    if any(run["mode"] != "local" for run in runs.values()):
        raise BundleError("Only local execution evidence is supported")
    values = {}
    for artifact_id, record in records.items():
        relative = _relative(index["artifacts"][artifact_id])
        value = indexed(relative)
        if record["run_id"] not in runs or _hash(raw_files[relative]) != record["hash"]:
            raise BundleError("Artifact ownership or original hash mismatch")
        if any(item not in records for item in record["inputs"]):
            raise BundleError("Artifact provenance dependency is missing")
        if isinstance(value, dict) and "metadata" in value:
            provider = value["metadata"].get("provider")
            if provider != "local_rules":
                raise BundleError("Provider provenance must identify local rules")
            rule_sources = value["metadata"].get("rule_sources", {})
            if provider == "local_rules" and not rule_sources:
                raise BundleError("Local rule provenance is missing")
            for name, expected_hash in rule_sources.items():
                relative = _relative(index.get("rules", {})[name])
                if relative not in raw_files or _hash(raw_files[relative]) != expected_hash:
                    raise BundleError("Recorded local rule source hash mismatch")
        if isinstance(value, dict) and "input_context" in value:
            actual = _hash(json.dumps(value["input_context"], sort_keys=True, separators=(",", ":")).encode())
            if actual != value["request_hash"]:
                raise BundleError("Recorded request context hash mismatch")
        if isinstance(value, dict) and "role_template" in value.get("prompt", {}):
            prompt = value["prompt"]
            relative = _relative(index["prompts"][prompt["role_template"]])
            if relative not in raw_files or _hash(raw_files[relative]) != prompt["role_template_hash"]:
                raise BundleError("Recorded prompt template hash mismatch")
        values[artifact_id] = value

    def reference(ref, run_id=None, revision=None):
        record = records[ref["id"]]
        if ref["hash"] != record["hash"] or (run_id is not None and record["run_id"] != run_id) or (revision is not None and record["revision"] != revision):
            raise BundleError("Artifact reference hash, owner, or revision mismatch")
        return values[ref["id"]]

    manifests = {}
    for hash_value, relative in index["candidates"].items():
        if not HEX.fullmatch(hash_value) or _relative(relative) != f"candidates/{hash_value}":
            raise BundleError("Invalid candidate index")
        manifests[hash_value] = candidate_manifest(root / relative)
        if manifests[hash_value]["hash"] != hash_value:
            raise BundleError("Candidate contents differ from content hash")

    def candidate(ref):
        expected = ref["manifest"]
        if manifests.get(expected["hash"]) != expected:
            raise BundleError("Referenced candidate is missing or changed")
        return str(root / index["candidates"][expected["hash"]])

    for run in runs.values():
        for key in ("base", "candidate"):
            if run["data"].get(key):
                candidate(run["data"][key])
        baseline = run["data"].get("baseline")
        if baseline:
            reference(baseline)
            prior = runs[records[baseline["id"]]["run_id"]]
            if prior["status"] != "succeeded" or prior["mode"] != run["mode"] or prior["data"]["release"]["id"] != baseline["id"]:
                raise BundleError("Baseline is not accepted in the same mode")
        handoff_ref = run["data"].get("handoff")
        if handoff_ref:
            handoff = reference(handoff_ref, run["id"], run["revision"])
            if (run["status"] != "failed" or handoff.get("run_id") != run["id"]
                    or handoff.get("revision") != run["revision"]
                    or handoff.get("budgets") != {key: run["data"][key] for key in ("calls", "active_seconds", "repairs", "replans")}):
                raise BundleError("Terminal handoff or spent budgets changed")
        recovery = run["data"].get("recovery")
        if recovery:
            parent = runs.get(recovery.get("parent_run"))
            if (not parent or parent["mode"] != run["mode"] or parent["scenario"] != run["scenario"]
                    or parent["status"] != "failed" or parent["revision"] != recovery.get("parent_revision")
                    or parent["data"].get("recovery") or parent["data"].get("recovery_child") != run["id"]):
                raise BundleError("Recovery parent lineage is missing or inconsistent")
            handoff_ref = parent["data"].get("handoff") or {}
            if (handoff_ref.get("id"), handoff_ref.get("hash")) != (recovery.get("handoff_id"), recovery.get("handoff_hash")):
                raise BundleError("Recovery does not bind the parent's handoff")
            if reference(handoff_ref, parent["id"], parent["revision"]).get("permitted_action") != "create_replacement":
                raise BundleError("Recovery bypasses a manual-review handoff")
            if not isinstance(recovery.get("instructions"), str) or not recovery["instructions"].strip():
                raise BundleError("Recovery instructions are missing")
            decisions = [item for item in metadata["approvals"]
                         if item["run_id"] == parent["id"] and item["revision"] == parent["revision"]
                         and item["action"] == "recover" and item["hash"] == handoff_ref["hash"]
                         and item["actor"] == recovery.get("actor") and item["actor"].strip()
                         and item["decision"] == "approve" and item["valid"]]
            expected_event = {"child_run": run["id"], **recovery}
            if len(decisions) != 1 or not any(
                event["run_id"] == parent["id"] and event["revision"] == parent["revision"]
                and event["kind"] == "recovery_created" and event["payload"] == expected_event
                for event in metadata["events"]
            ):
                raise BundleError("Recovery lacks its bound operator decision")
        child_id = run["data"].get("recovery_child")
        if child_id and (child_id not in runs or runs[child_id]["data"].get("recovery", {}).get("parent_run") != run["id"]):
            raise BundleError("Recovery child lineage is missing or inconsistent")
    for start in runs:
        seen, current = set(), start
        while runs[current]["data"].get("baseline"):
            if current in seen:
                raise BundleError("Accepted baseline lineage contains a cycle")
            seen.add(current)
            current = records[runs[current]["data"]["baseline"]["id"]]["run_id"]
    for node in metadata["nodes"]:
        if node["run_id"] not in runs or not 1 <= node["revision"] <= runs[node["run_id"]]["revision"]:
            raise BundleError("Node revision or ownership mismatch")
        result = node["result"]
        if isinstance(result, dict):
            if "manifest" in result:
                candidate(result)
            if result.get("candidate"):
                candidate(result["candidate"])
            if "id" in result and "hash" in result:
                reference(result, node["run_id"], node["revision"])
    for artifact_id, record in records.items():
        value = values[artifact_id]
        if record["node_id"] == "release":
            candidate(value["candidate"])
            if value["mode"] != runs[record["run_id"]]["mode"] or value["revision"] != record["revision"]:
                raise BundleError("Historical release mode or revision mismatch")
            for name in ("plan", "review", "validation", "documentation"):
                reference(value[name], record["run_id"], record["revision"])
        if isinstance(value, dict) and "evidence_dir" in value and "evidence_hashes" in value and artifact_id not in index["evidence"]:
            raise BundleError("Validation evidence mapping is missing")
    for key, mapping in index["evidence"].items():
        for original, relative in mapping["files"].items():
            _relative(original)
            if _relative(relative) != f"evidence/{key}/{original}" or relative not in raw_files:
                raise BundleError("Invalid external evidence mapping")
        if key in values:
            hashes = values[key]["evidence_hashes"]
            included = {name: value for name, value in hashes.items() if not _excluded(name)}
            excluded = sorted(name for name in hashes if _excluded(name))
            if set(included) != set(mapping["files"]) or excluded != mapping["excluded"]:
                raise BundleError("Evidence export omitted a required historical file")
            for original, expected in included.items():
                if _hash(raw_files[mapping["files"][original]]) != expected:
                    raise BundleError("Historical evidence hash mismatch")
    for event in metadata["events"]:
        if event["kind"] == "rollback_smoke" and isinstance(event["payload"].get("report"), str):
            mapping = index["evidence"].get(f"event-{event['seq']}", {}).get("files", {})
            if "report.json" not in mapping:
                raise BundleError("Rollback smoke evidence is missing its report")
            report = indexed(mapping["report.json"])
            if not _smoke_required_files(report).issubset(mapping):
                raise BundleError("Rollback smoke evidence is missing required files")
            if event["payload"].get("passed") is not report["passed"]:
                raise BundleError("Rollback smoke outcome differs from its report")
    acceptance = _relative(index["acceptance"])
    expected_tests = indexed(f"{acceptance}/manifest.json")
    fingerprint = _trusted_fingerprint(root / acceptance)
    accepted = []
    for run_id, run in runs.items():
        if run["status"] != "succeeded":
            continue
        revision, mode = run["revision"], run["mode"]
        release_ref = run["data"]["release"]
        release = reference(release_ref, run_id, revision)
        if release["mode"] != mode or release["revision"] != revision:
            raise BundleError("Accepted release mode/revision mismatch")
        candidate_path = candidate(release["candidate"])
        validation = reference(release["validation"], run_id, revision)
        review = reference(release["review"], run_id, revision)
        reference(release["documentation"], run_id, revision)
        reference(release["plan"], run_id, revision)
        if validation.get("passed") is not True or validation.get("candidate_hash") != release["candidate"]["manifest"]["hash"]:
            raise BundleError("Accepted release lacks successful matching validation")
        bounds = (run["data"]["alias_min"], run["data"]["alias_max"])
        if any(type(value) is not int for value in bounds) or not 4 <= bounds[0] <= bounds[1] <= 32:
            raise BundleError("Invalid accepted alias bounds")
        inputs = {"candidate_hash": validation["candidate_hash"], "scenario": run["scenario"],
                  "alias_min": bounds[0], "alias_max": bounds[1]}
        input_hash = _hash(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode())
        if validation.get("input_hash") != input_hash:
            raise BundleError("Accepted validation input hash differs from scenario or alias bounds")
        if validation.get("mode") != "local" or not validation.get("image_id"):
            raise BundleError("Validation evidence must identify local Docker execution")
        state = validation.get("container_state", {})
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", validation["image_id"]) or state.get("Status") != "exited" or state.get("Running") is not False or state.get("ExitCode") != 0 or validation.get("cli_exit_code") != 0:
            raise BundleError("Generated validation lacks successful Docker exit evidence")
        if any(finding["severity"] == "blocking" for finding in review["output"]["findings"]):
            raise BundleError("Accepted release has a blocking review finding")
        mapping = index["evidence"][release["validation"]["id"]]["files"]
        envelope = indexed(mapping["stdout.log"])
        assessed, _ = DockerRunner._assess(envelope, expected_tests[run["scenario"]], fingerprint, validation["exit_code"])
        if not assessed["passed"]:
            raise BundleError("Archived validation cannot substantiate a successful release")
        for field in ("tests", "failures", "errors", "skipped", "protected_test_ids"):
            if validation.get(field) != assessed[field]:
                raise BundleError("Historical validation totals differ from runner evidence")

        def approval(action, hash_value):
            matches = [row for row in metadata["approvals"] if row["run_id"] == run_id and row["revision"] == revision
                       and row["action"] == action and row["hash"] == hash_value and row["decision"] == "approve"
                       and row["valid"] == 1 and isinstance(row["actor"], str) and row["actor"].strip()]
            if not matches:
                raise BundleError("Accepted release has no matching valid human approval")
            if not any(event["run_id"] == run_id and event["revision"] == revision and event["kind"] == "human_decision"
                       and event["payload"].get("action") == action and event["payload"].get("hash") == hash_value
                       and event["payload"].get("decision") == "approve" and event["payload"].get("actor") in {row["actor"] for row in matches}
                       for event in metadata["events"]):
                raise BundleError("Approval lacks its historical decision event")
        approval("accept", release_ref["hash"])
        if run["data"].get("baseline") or run["scenario"] != "greenfield":
            approval("plan", release["plan"]["hash"])
        accepted.append({"run_id": run_id, "mode": mode, "scenario": run["scenario"], "revision": revision,
                         "alias_min": run["data"]["alias_min"], "alias_max": run["data"]["alias_max"],
                         "candidate_path": candidate_path, "candidate_hash": release["candidate"]["manifest"]["hash"]})
    for selection in metadata["accepted"]:
        run = runs[selection["run_id"]]
        release = run["data"]["release"]
        if run["status"] != "succeeded" or selection["mode"] != run["mode"] or selection["hash"] != release["hash"] or selection["artifact_id"] != release["id"]:
            raise BundleError("Selected release metadata is inconsistent")
    return {"verified": True, "format": FORMAT, "bundle_path": str(root), "notice": NOTICE,
            "runs": sorted(runs), "requested_runs": document["requested_runs"], "accepted": accepted,
            "files": len(inventory), "bytes": size}
