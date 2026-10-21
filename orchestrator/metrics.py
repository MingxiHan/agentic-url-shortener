"""Observed local workflow samples from retained execution events."""

from collections import Counter
from datetime import datetime


TERMINAL = {"succeeded", "failed", "stopped"}
STATUSES = ("running", "waiting_for_input", "waiting_for_approval", "succeeded", "failed", "stopped")


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else "N/A"


def _seconds(later, earlier):
    return max(0.0, (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds())


def metrics(store) -> dict:
    """Derive counts from retained events; an accepted run requires a human decision."""
    groups = {
        name: {"runs": 0, "statuses": dict.fromkeys(STATUSES, 0), "terminal_runs": 0,
               "accepted_runs": 0, "started_attempts": 0, "additional_attempts": 0,
               "repairs": 0, "rollbacks": 0, "unresolved_failure_episodes": 0,
               "recovery_seconds": [], "terminal_durations": []}
        for name in ("local",)
    }
    for run in store.list_runs():
        if run["mode"] != "local":
            raise ValueError("Only local execution evidence is supported")
        group = groups["local"]
        group["runs"] += 1
        group["statuses"][run["status"]] = group["statuses"].get(run["status"], 0) + 1
        attempts, pending = Counter(), {}
        status, terminal_at, accepted = "running", None, False
        for event in store.events(run["id"]):
            kind, payload, revision = event["kind"], event["payload"], event["revision"]
            stamp = event["timestamp"]
            if kind == "node_running":
                attempts[(revision, payload["node"])] += 1
            elif kind == "repair_applied":
                group["repairs"] += 1
            elif kind == "rollback":
                group["rollbacks"] += 1
            elif kind == "validation_failed":
                pending.setdefault(revision, stamp)
            elif kind == "recovery_completed" and revision in pending:
                group["recovery_seconds"].append(_seconds(stamp, pending.pop(revision)))
            next_status = payload.get("status")
            if kind == "human_decision":
                if payload.get("decision") == "reject":
                    next_status = "stopped"
                elif payload.get("action") == "accept" and payload.get("decision") == "approve":
                    next_status, accepted = "succeeded", True
                elif payload.get("action") == "plan" and payload.get("decision") == "approve":
                    next_status = "running"
            elif kind == "requirement_revised":
                next_status = "running"
            if next_status in STATUSES:
                if next_status in TERMINAL and next_status != status:
                    terminal_at = stamp
                status = next_status
        group["started_attempts"] += sum(attempts.values())
        group["additional_attempts"] += sum(count - 1 for count in attempts.values())
        group["unresolved_failure_episodes"] += len(pending)
        if run["status"] in TERMINAL:
            group["terminal_runs"] += 1
            group["accepted_runs"] += int(run["status"] == "succeeded" and accepted)
            elapsed = _seconds(terminal_at or run["updated_at"], run["created_at"])
            active = max(0.0, float(run["data"].get("active_seconds", 0)))
            group["terminal_durations"].append({
                "run_id": run["id"], "end_to_end_seconds": elapsed,
                "active_seconds": active, "waiting_seconds": max(0.0, elapsed - active),
            })
    for group in groups.values():
        group["success_rate"] = _ratio(group["accepted_runs"], group["terminal_runs"])
        group["retry_frequency"] = _ratio(group["additional_attempts"], group["started_attempts"])
        group["rollback_frequency"] = _ratio(group["rollbacks"], group["terminal_runs"])
        recovery = group["recovery_seconds"]
        group["recovery_samples"] = len(recovery)
        group["mttr_proxy_seconds"] = _ratio(sum(recovery), len(recovery))
        durations = group["terminal_durations"]
        for key in ("end_to_end_seconds", "active_seconds", "waiting_seconds"):
            group["mean_" + key] = _ratio(sum(item[key] for item in durations), len(durations))
    return {
        "groups": groups,
        "note": "Observed local-rule samples, not reliability guarantees or model evidence. "
                "MTTR is a workflow recovery proxy. Waiting is wall-clock minus recorded active time "
                "and includes operator/idle delays. Recovery episodes are scoped to a requirement revision.",
    }
