"""Exercise requirement-driven local rules with real Docker validation and retained evidence.

Human inputs in this reproducible demonstration are explicitly scripted. The CLI
also supports stopping at each gate for an actual operator's separate decision.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from orchestrator.artifacts import snapshot_files
from orchestrator.bundle import export_bundle
from orchestrator.domain import APP_PATH, inspect_source
from orchestrator.engine import Engine, GateFailure
from orchestrator.local_provider import LocalProvider
from orchestrator.metrics import metrics
from orchestrator.runner import DockerRunner
from orchestrator.store import StateConflict, Store


ACTOR = "scripted-local-demo-operator"


async def run_scenarios(root, image):
    if root.exists() or root.is_symlink() or root.parent.resolve() != root.parent:
        raise ValueError("Use a fresh retained run directory without symlink parents")
    store = Store(root)
    worker = Engine(store, LocalProvider(), DockerRunner(image, mode="local"))
    environment = {"runner": worker.runner.preflight(), "provider": worker.provider.preflight()}
    print("LOCAL RULE DEMO: actual source transformations and Docker tests; zero API calls. Operator inputs are scripted.", flush=True)

    async def advance(run_id):
        run = await worker.run(run_id)
        if run["status"] == "waiting_for_approval" and run["data"]["pending"]["action"] == "plan":
            pending = run["data"]["pending"]
            print(f"Scripted plan approval: {run_id} {pending['hash']}", flush=True)
            worker.approve(run_id, "plan", pending["hash"], ACTOR)
            run = await worker.run(run_id)
        return run

    def accept(run):
        if run["status"] != "waiting_for_approval" or run["data"]["pending"]["action"] != "accept":
            raise RuntimeError(f"Unexpected scenario outcome: {run['status']}: {run['data'].get('error')}")
        worker.verify_release(run["data"]["release"]["id"])
        print(f"Scripted release approval: {run['id']} {run['data']['pending']['hash']}", flush=True)
        worker.approve(run["id"], "accept", run["data"]["pending"]["hash"], ACTOR)
        return store.get(run["id"])

    def describe(run):
        release = worker.verify_release(run["data"]["release"]["id"])
        source = snapshot_files(Path(release["candidate"]["path"]))
        baseline = snapshot_files(Path(run["data"]["base"]["path"]))
        validation = store.read_artifact(release["validation"]["id"])
        return {"id": run["id"], "revision": run["revision"], "status": run["status"],
                "requirement": run["data"]["requirement"], "answer": run["data"]["answers"],
                "before": inspect_source(baseline), "after": inspect_source(source),
                "before_app_hash": hashlib.sha256(baseline[APP_PATH].encode()).hexdigest(),
                "after_app_hash": hashlib.sha256(source[APP_PATH].encode()).hexdigest(),
                "plan": store.read_artifact(release["plan"]["id"])["output"],
                "tests": validation["tests"], "passed": validation["passed"],
                "report": validation["report_path"], "candidate": release["candidate"]["manifest"]["hash"]}

    green = worker.create("greenfield")
    green = accept(await advance(green["id"]))
    green_evidence = describe(green)
    assert not green_evidence["before"]["core"]
    assert green_evidence["after"]["core"] and not green_evidence["after"]["expiration"] and not green_evidence["after"]["aliases"]

    brown = worker.create("brownfield", baseline=green["id"])
    brown = accept(await advance(brown["id"]))
    brown_evidence = describe(brown)
    assert not brown_evidence["before"]["expiration"] and brown_evidence["after"]["expiration"]
    assert brown_evidence["before_app_hash"] != brown_evidence["after_app_hash"]
    worker.rollback(green["id"], store.selected("local")["hash"], ACTOR)
    selected = worker.verify_release(store.selected("local")["artifact_id"])
    smoke = worker.runner.validate(Path(selected["candidate"]["path"]), root / brown["id"] / "rollback-smoke", "greenfield")
    store.event(brown["id"], brown["revision"], "rollback_smoke", {"passed": smoke["passed"], "target": green["id"], "report": smoke["report_path"]})
    if not smoke["passed"]:
        raise RuntimeError("Actual rollback smoke validation failed")

    ambiguous = worker.create("ambiguous", baseline=green["id"])
    ambiguous = await advance(ambiguous["id"])
    if ambiguous["status"] != "waiting_for_input":
        raise RuntimeError("Unresolved sharing requirement must ask for clarification")
    questions = ambiguous["data"]["questions"]
    with store.lock(ambiguous["id"]):
        store.revise(ambiguous["id"], "Use optional custom aliases, exactly eight characters; return 409 on collisions.")
    ambiguous = await advance(ambiguous["id"])
    if ambiguous["status"] != "waiting_for_approval":
        raise RuntimeError(f"Alias clarification failed: {ambiguous['data'].get('error')}")
    first_alias = describe(ambiguous)
    old_hash = ambiguous["data"]["pending"]["hash"]
    with store.lock(ambiguous["id"]):
        store.revise(ambiguous["id"], "Change the approved alias acceptance rule to 4-32 characters; retain duplicate 409 and random defaults.")
    try:
        worker.approve(ambiguous["id"], "accept", old_hash, ACTOR)
    except (StateConflict, GateFailure):
        store.event(ambiguous["id"], 3, "stale_approval_rejected", {"hash": old_hash})
    else:
        raise RuntimeError("Superseded alias approval was accepted")
    ambiguous = accept(await advance(ambiguous["id"]))
    ambiguous_evidence = describe(ambiguous)
    assert (first_alias["after"]["alias_min"], first_alias["after"]["alias_max"]) == (8, 8)
    assert (ambiguous_evidence["after"]["alias_min"], ambiguous_evidence["after"]["alias_max"]) == (4, 32)
    assert first_alias["after_app_hash"] != ambiguous_evidence["after_app_hash"]

    limited = Engine(store, LocalProvider(), DockerRunner(image, mode="local"), max_calls=2)
    failed = limited.create("brownfield", baseline=ambiguous["id"])
    failed = await limited.run(failed["id"])
    if failed["status"] == "waiting_for_approval":
        limited.approve(failed["id"], "plan", failed["data"]["pending"]["hash"], ACTOR)
        failed = await limited.run(failed["id"])
    if failed["status"] != "failed" or not failed["data"].get("handoff"):
        raise RuntimeError("Deliberately limited budget did not produce the expected retained handoff")
    handoff = store.read_artifact(failed["data"]["handoff"]["id"])
    replacement = worker.recover(failed["id"], failed["data"]["handoff"]["hash"], ACTOR,
                                 "Use the standard bounded budget; preserve the API and current accepted source.")
    replacement = accept(await advance(replacement["id"]))
    replacement_evidence = describe(replacement)
    assert store.get(failed["id"])["status"] == "failed"
    assert store.get(failed["id"])["data"]["calls"] == failed["data"]["calls"] == 2
    assert replacement["data"]["recovery"]["parent_run"] == failed["id"]
    assert replacement_evidence["after"]["aliases"] and replacement_evidence["after"]["expiration"]
    summary = {"mode": "local", "model_calls": 0, "operator": ACTOR,
               "notice": "Actual bounded rule execution and real Docker tests. Reusable authored fragments and scripted operator decisions; no LLM synthesis or general reasoning claim.",
               "environment": environment, "greenfield": green_evidence, "brownfield": brown_evidence,
               "rollback": {"passed": smoke["passed"], "tests": smoke["tests"], "report": smoke["report_path"]},
               "ambiguous": {"questions": questions, "first_clarification": first_alias, "final": ambiguous_evidence},
               "fallback": {"trigger": "Deliberately restricted two-call budget", "parent": failed["id"],
                            "handoff": handoff, "recovered": replacement_evidence},
               "metrics": metrics(store)}
    (root / "scenario-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    bundle = export_bundle(store, [green["id"], brown["id"], ambiguous["id"], replacement["id"]], root / "portable-bundle")
    (root / "export.json").write_text(json.dumps(bundle, indent=2) + "\n")
    print(json.dumps({"summary": str(root / "scenario-summary.json"), "bundle": str(root / "portable-bundle"),
                      "verified": bundle["verified"], "runs": bundle["runs"]}, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, required=True, help="Fresh directory; retained after the demonstration")
    parser.add_argument("--image", default="agentic-shortener-runner:local")
    args = parser.parse_args()
    asyncio.run(run_scenarios(args.runs_dir.absolute(), args.image))


if __name__ == "__main__":
    main()
