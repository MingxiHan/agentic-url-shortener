"""Offline engineering workflow with explicit human approval gates."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .engine import Engine
from .local_provider import LocalProvider
from .runner import DockerRunner
from .store import Store


def make_engine(store, mode="local"):
    if mode != "local":
        raise ValueError("Only local execution is supported")
    image = os.getenv("AGENT_RUNNER_IMAGE", "agentic-shortener-runner:local")
    return Engine(store, LocalProvider(), DockerRunner(image, mode="local"))


def print_run(run):
    data = run["data"]
    print(json.dumps({"id": run["id"], "mode": run["mode"], "scenario": run["scenario"],
                      "status": run["status"], "revision": run["revision"],
                      "pending": data.get("pending"), "questions": data.get("questions"),
                      "error": data.get("error"), "release": data.get("release"),
                      "handoff": data.get("handoff"), "recovery": data.get("recovery"),
                      "recovery_child": data.get("recovery_child")}, indent=2))


def parser():
    cli = argparse.ArgumentParser(description="Bounded agentic URL shortener workflow")
    cli.add_argument("--runs-dir")
    commands = cli.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Check the installed local prerequisites")
    doctor.add_argument("--mode", choices=["local"], default="local")
    start = commands.add_parser("start")
    start.add_argument("scenario", choices=["greenfield", "brownfield", "ambiguous"])
    start.add_argument("--mode", choices=["local"], default="local")
    start.add_argument("--requirement")
    start.add_argument("--baseline")
    for name in ("inspect", "events", "resume", "stop"):
        commands.add_parser(name).add_argument("run")
    approve = commands.add_parser("approve")
    approve.add_argument("run")
    approve.add_argument("--action", choices=["plan", "accept"], required=True)
    approve.add_argument("--hash", required=True)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--reject", action="store_true")
    revise = commands.add_parser("answer", help="Record a human clarification or changed requirement")
    revise.add_argument("run")
    revise.add_argument("--text", required=True)
    revise.add_argument("--alias-min", type=int)
    revise.add_argument("--alias-max", type=int)
    recover = commands.add_parser("recover", help="Authorize one linked replacement; resume the returned child to execute it")
    recover.add_argument("run")
    recover.add_argument("--hash", required=True, help="Exact retained handoff artifact hash")
    recover.add_argument("--actor", required=True)
    recover.add_argument("--instructions", required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("target_run")
    rollback.add_argument("--expected-current", required=True)
    rollback.add_argument("--actor", required=True)
    commands.add_parser("metrics")
    commands.add_parser("list")
    export = commands.add_parser("export", help="Export retained runs and dependencies to a new portable evidence directory")
    export.add_argument("--run", action="append", required=True, dest="run_ids")
    export.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-bundle", help="Check portable evidence without using a model or original run store")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--rerun", action="store_true", help="Also execute accepted candidates in Docker")
    verify.add_argument("--output", type=Path, help="Fresh report directory required with --rerun")
    return cli


def verify_command(args):
    from .artifacts import manifest
    from .bundle import verify_bundle

    checked = verify_bundle(args.bundle)
    if not args.rerun:
        if args.output:
            raise ValueError("--output is only used with --rerun")
        print(json.dumps(checked, indent=2))
        return 0
    if args.output is None:
        raise ValueError("--rerun requires --output pointing to a fresh directory")
    destination = args.output.absolute()
    bundle = args.bundle.resolve()
    if destination.exists() or destination.is_symlink() or destination.resolve().is_relative_to(bundle):
        raise ValueError("Rerun output must be a new directory outside the evidence bundle")
    if destination.parent.resolve() != destination.parent:
        raise ValueError("Rerun output parents must not contain symlinks")
    entries = checked["accepted"]
    if not entries:
        raise ValueError("The bundle has no accepted candidates to execute")
    if any(entry["mode"] != "local" for entry in entries):
        raise ValueError("Only local candidate evidence can be executed")
    runner = DockerRunner(os.getenv("AGENT_RUNNER_IMAGE", "agentic-shortener-runner:local"), mode="local")
    runner.preflight()
    destination.mkdir(parents=True)
    results = []
    for entry in entries:
        candidate = Path(entry["candidate_path"])
        if manifest(candidate)["hash"] != entry["candidate_hash"]:
            raise ValueError("Candidate changed after bundle verification")
        report = runner.validate(
            candidate, destination / entry["run_id"], entry["scenario"],
            entry["alias_min"], entry["alias_max"],
        )
        if manifest(candidate)["hash"] != entry["candidate_hash"]:
            raise ValueError("Candidate changed during validation")
        results.append({"run_id": entry["run_id"], "mode": entry["mode"],
                        "passed": report["passed"], "tests": report["tests"],
                        "report_path": report["report_path"], "summary": report["summary"]})
    result = {"evidence_integrity": "verified", "model_calls": 0, "reruns": results,
              "notice": "Fresh execution of submitted candidates; no new model generation."}
    (destination / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return int(any(not item["passed"] for item in results))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "verify-bundle":
            return verify_command(args)
        store = Store(Path(args.runs_dir or os.getenv("AGENT_RUNS_DIR", "runs")))
        if args.command == "doctor":
            engine = make_engine(store, args.mode)
            result = {"mode": args.mode}
            for name, component in (("runner", engine.runner), ("provider", engine.provider)):
                try:
                    result[name] = component.preflight()
                except Exception as error:
                    result[name] = {"available": False, "error": str(error)}
            print(json.dumps(result, indent=2))
            return int(any(isinstance(value, dict) and value.get("available") is False for value in result.values()))
        if args.command == "start":
            engine = make_engine(store, args.mode)
            run = engine.create(args.scenario, args.requirement, args.baseline)
            run = asyncio.run(engine.run(run["id"]))
            print_run(run)
            return int(run["status"] == "failed")
        elif args.command in ("inspect", "resume", "stop", "approve", "answer", "events", "recover"):
            run = store.get(args.run)
            engine = make_engine(store, run["mode"])
            if args.command == "resume":
                run = asyncio.run(engine.run(args.run))
                print_run(run)
                return int(run["status"] == "failed")
            if args.command == "recover":
                print_run(engine.recover(args.run, args.hash, args.actor, args.instructions))
                return 0
            if args.command == "stop":
                store.stop(args.run)
            elif args.command == "approve":
                engine.approve(args.run, args.action, args.hash, args.actor, "reject" if args.reject else "approve")
            elif args.command == "answer":
                with store.lock(args.run):
                    store.revise(args.run, args.text, alias_min=args.alias_min, alias_max=args.alias_max)
            elif args.command == "events":
                for event in store.events(args.run):
                    print(json.dumps(event, sort_keys=True))
                return 0
            if args.command == "inspect":
                print(json.dumps({"run": store.get(args.run), "nodes": store.nodes(args.run)}, indent=2))
            else:
                print_run(store.get(args.run))
        elif args.command == "rollback":
            run = store.get(args.target_run)
            make_engine(store, run["mode"]).rollback(args.target_run, args.expected_current, args.actor)
            print(json.dumps(store.selected(run["mode"]), indent=2))
        elif args.command == "metrics":
            from .metrics import metrics
            print(json.dumps(metrics(store), indent=2))
        elif args.command == "list":
            for run in store.list_runs():
                print_run(run)
        elif args.command == "export":
            from .bundle import export_bundle
            print(json.dumps(export_bundle(store, args.run_ids, args.output), indent=2))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
