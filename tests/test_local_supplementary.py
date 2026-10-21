"""Run authored supplementary checks against trusted rule-built service variants."""

import asyncio
from pathlib import Path
import sys
import types

import pytest

from orchestrator.contracts import SCENARIOS, contract
from orchestrator.domain import APP_PATH, apply_operation
from orchestrator.local_provider import LocalProvider
from orchestrator.models import ChangeSet


ROOT = Path(__file__).resolve().parents[1]


def execute_authored_check(files, source, directory, monkeypatch):
    # These inputs come only from committed domain rules and authored test templates.
    module = types.ModuleType("shortener.app")
    exec(compile(files[APP_PATH], APP_PATH, "exec"), module.__dict__)
    monkeypatch.setitem(sys.modules, "shortener", types.ModuleType("shortener"))
    monkeypatch.setitem(sys.modules, "shortener.app", module)
    namespace = {}
    exec(compile(source, "test_rule_contract.py", "exec"), namespace)
    directory.mkdir()
    names = [name for name in namespace if name.startswith("test_rule_")]
    for name in names:
        namespace[name](directory)
    return names


@pytest.mark.parametrize("operations", [(), ("expiration_schema", "expiration"), ("aliases",),
                                        ("expiration_schema", "expiration", "aliases")])
def test_emitted_core_check_preserves_existing_features_and_rejects_feature_drift(tmp_path, monkeypatch, operations):
    core = {APP_PATH: (ROOT / "skeleton/shortener/app.py").read_text()}
    core.update(apply_operation(core, "core"))
    baseline = dict(core)
    for operation in operations:
        baseline.update(apply_operation(baseline, operation))
    context = {"scenario": "greenfield", "requirement": SCENARIOS["greenfield"], "answers": "",
               "contract": contract("greenfield"), "files": baseline}
    changes, _ = asyncio.run(LocalProvider().respond("verifier", context, ChangeSet))
    source = changes.changes[0].content
    execute_authored_check(baseline, source, tmp_path / "preserved", monkeypatch)
    changed = dict(core)
    if not operations:
        changed.update(apply_operation(changed, "aliases"))
    with pytest.raises(AssertionError):
        execute_authored_check(changed, source, tmp_path / "changed", monkeypatch)


def composed_candidate(goal, *, existing_aliases=True, target_bounds=(8, 8)):
    baseline = {APP_PATH: (ROOT / "skeleton/shortener/app.py").read_text()}
    baseline.update(apply_operation(baseline, "core"))
    if existing_aliases:
        baseline.update(apply_operation(baseline, "aliases", alias_min=8, alias_max=8))
    if goal == "aliases":
        for operation in ("expiration_schema", "expiration"):
            baseline.update(apply_operation(baseline, operation))
    scenario = "brownfield" if goal == "expiration" else "ambiguous"
    answer = "" if goal == "expiration" else (
        f"Use optional custom aliases, {target_bounds[0]}-{target_bounds[1]} characters; duplicate aliases return 409.")
    context = {"scenario": scenario, "requirement": SCENARIOS[scenario], "answers": answer,
               "contract": contract(scenario, *target_bounds), "files": baseline}
    changes, _ = asyncio.run(LocalProvider().respond("verifier", context, ChangeSet))
    candidate = dict(baseline)
    if goal == "expiration":
        for operation in ("expiration_schema", "expiration"):
            candidate.update(apply_operation(candidate, operation))
    else:
        candidate.update(apply_operation(candidate, "aliases", alias_min=target_bounds[0], alias_max=target_bounds[1]))
    return candidate, changes.changes[0].content


@pytest.mark.parametrize("goal,existing_aliases,target_bounds,old,new", [
    ("expiration", True, (8, 8), 'status_code=409, detail="alias_already_exists"', 'status_code=400, detail="alias_already_exists"'),
    ("expiration", True, (8, 8), "not alias_min <= len(request.alias) <= alias_max", "False"),
    ("aliases", False, (8, 8), 'status_code=410, detail="link_expired"', 'status_code=404, detail="link_expired"'),
    ("aliases", True, (4, 32), 'status_code=410, detail="link_expired"', 'status_code=404, detail="link_expired"'),
])
def test_goal_and_existing_feature_behavior_are_both_checked(
    tmp_path, monkeypatch, goal, existing_aliases, target_bounds, old, new,
):
    candidate, source = composed_candidate(goal, existing_aliases=existing_aliases, target_bounds=target_bounds)
    monkeypatch.setenv("ALIAS_MIN_LENGTH", str(target_bounds[0]))
    monkeypatch.setenv("ALIAS_MAX_LENGTH", str(target_bounds[1]))
    assert source.count("def test_rule_clarified_alias_bounds(") == 1
    names = execute_authored_check(candidate, source, tmp_path / "preserved", monkeypatch)
    assert set(names) == {"test_rule_expiration_addition", "test_rule_clarified_alias_bounds"}
    assert candidate[APP_PATH].count(old) == 1
    broken = {**candidate, APP_PATH: candidate[APP_PATH].replace(old, new)}
    with pytest.raises(AssertionError):
        execute_authored_check(broken, source, tmp_path / "broken", monkeypatch)


def test_existing_alias_bounds_are_not_silently_overridden_by_a_different_run_environment(tmp_path, monkeypatch):
    candidate, source = composed_candidate("expiration")
    monkeypatch.setenv("ALIAS_MIN_LENGTH", "8")
    monkeypatch.setenv("ALIAS_MAX_LENGTH", "8")
    execute_authored_check(candidate, source, tmp_path / "correct-contract", monkeypatch)
    monkeypatch.setenv("ALIAS_MIN_LENGTH", "4")
    monkeypatch.setenv("ALIAS_MAX_LENGTH", "32")
    with pytest.raises(AssertionError):
        execute_authored_check(candidate, source, tmp_path / "wrong-contract", monkeypatch)
