"""Requirement text and inspected source drive the bounded local execution path."""

import asyncio
from pathlib import Path

import pytest

from orchestrator.contracts import SCENARIOS, contract
from orchestrator.domain import APP_PATH, DomainError, apply_operation, inspect_source
from orchestrator.local_provider import LocalProvider, interpret
from orchestrator.models import ChangeSet, Documentation, Plan, Requirement, Review


ROOT = Path(__file__).resolve().parents[1]


def context(scenario="greenfield", requirement=None, answer="", files=None):
    return {"scenario": scenario, "requirement": requirement or SCENARIOS[scenario], "answers": answer,
            "contract": contract(scenario), "files": files or {APP_PATH: (ROOT / "skeleton/shortener/app.py").read_text()}}


def respond(role, value, schema):
    return asyncio.run(LocalProvider().respond(role, value, schema))[0]


@pytest.mark.parametrize("requirement_text", [SCENARIOS["greenfield"], "Create a core URL shortener with redirects and counts."])
def test_supported_paraphrases_resolve_core_intent(requirement_text):
    result = interpret(context(requirement=requirement_text))
    assert result["goal"] == "core" and not result["questions"]


@pytest.mark.parametrize("requirement_text", ["Build a URL shortener with billing.", "Build a URL shortener without health.",
                                    "Build a URL shortener with 16 character random codes.", "Add expiration and aliases."])
def test_unsupported_or_conflicting_scope_cannot_silently_proceed(requirement_text):
    value = context(requirement=requirement_text)
    assert respond("normalize", value, Requirement).questions
    with pytest.raises(DomainError, match="Unresolved"):
        respond("planner", value, Plan)


def test_ambiguity_and_conflicting_alias_constraints_require_answers():
    assert respond("normalize", context("ambiguous"), Requirement).questions
    value = context("ambiguous", answer="Use custom aliases exactly 8 characters and 4-32 characters; duplicate aliases return 409.")
    assert respond("normalize", value, Requirement).questions


def test_actual_baseline_drives_schema_and_api_dependency_plan():
    initial = context()
    plan = respond("planner", initial, Plan)
    assert [task.id for task in plan.tasks] == ["task_core", "task_contract_tests"]
    built = respond("builder", {**initial, "task": plan.tasks[0].model_dump()}, ChangeSet)
    files = {**initial["files"], **{item.path: item.content for item in built.changes}}
    inventory = inspect_source(files)
    assert inventory["core"] and not inventory["expiration"] and not inventory["aliases"]
    brown = context("brownfield", files=files)
    plan = respond("planner", brown, Plan)
    assert [task.id for task in plan.tasks] == ["task_expiration_schema", "task_expiration", "task_contract_tests"]
    assert plan.tasks[1].dependencies == ["task_expiration_schema"]
    assert "links schema" in plan.rationale and "redirect" in plan.rationale
    migrated = {**files, **apply_operation(files, "expiration_schema")}
    plan = respond("planner", context("brownfield", files=migrated), Plan)
    assert [task.id for task in plan.tasks] == ["task_expiration", "task_contract_tests"]
    completed = {**migrated, **apply_operation(migrated, "expiration")}
    plan = respond("planner", context("brownfield", files=completed), Plan)
    assert [task.id for task in plan.tasks] == ["task_verify_existing", "task_contract_tests"]


def test_answer_changes_normalized_bounds_source_and_supplementary_checks():
    skeleton = context()["files"]
    files = {**skeleton, **apply_operation(skeleton, "core")}
    results = []
    for phrase, bounds in [("exactly eight characters", (8, 8)), ("4-32 characters", (4, 32))]:
        value = context("ambiguous", answer=f"Use optional custom aliases, {phrase}; return 409 on collisions.", files=files)
        normalized = respond("normalize", value, Requirement)
        assert not normalized.questions and (normalized.alias_min, normalized.alias_max) == bounds
        plan = respond("planner", value, Plan)
        changes = respond("builder", {**value, "task": plan.tasks[0].model_dump()}, ChangeSet)
        candidate = {**files, **{item.path: item.content for item in changes.changes}}
        inventory = inspect_source(candidate)
        assert (inventory["alias_min"], inventory["alias_max"]) == bounds
        tests = respond("verifier", value, ChangeSet)
        assert not respond("reviewer", {**value, "files": candidate}, Review).findings
        results.append((changes.changes[0].content, tests.changes[0].content))
    assert results[0][0] != results[1][0] and results[0][1] != results[1][1]


def test_unknown_repair_does_not_replace_failed_source_with_a_reference():
    skeleton = context()["files"]
    files = {**skeleton, **apply_operation(skeleton, "core")}
    with pytest.raises(DomainError, match="No safe automatic rule"):
        respond("builder", {**context(files=files), "task": "Repair arbitrary failed code"}, ChangeSet)


def test_documenter_uses_recorded_validation_and_rules_have_provenance():
    value = {**context(), "files": {}, "validation": {"passed": True, "summary": "Actual test report supplied by engine"}}
    output, metadata = asyncio.run(LocalProvider().respond("documenter", value, Documentation))
    assert "Actual test report" in output.rationale
    assert metadata["model_calls"] == 0 and metadata["provider"] == "local_rules"
    assert "orchestrator/domain.py" in metadata["rule_sources"]
    assert "orchestrator/templates/core.py.txt" in metadata["rule_sources"]
