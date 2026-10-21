"""Bounded English requirement rules and source transformations; no model inference.

This backend supports core shorteners, optional UTC expiration, and clarified
custom aliases. It derives work from text slots and the inspected baseline. It
does not interpret arbitrary English, reason about arbitrary repositories, or
repair unknown defects. Unsupported input asks for clarification or stops safely.
"""

import asyncio
import hashlib
import re
from pathlib import Path

from .domain import DomainError, apply_operation, inspect_source
from .models import ChangeSet, Documentation, Plan, Requirement, Review


CORE_REQUESTS = (
    r"(?:build|create|implement) a url shortener with creation, redirects, aggregate counts and health",
    r"(?:build|create|implement) a core url shortener with redirects and counts",
)
EXPIRATION_REQUEST = r"add optional link expiration while preserving existing links and counts"
AMBIGUOUS_REQUEST = r"make links easier to share"
NUMBER = r"(?:[0-9]{1,2}|eight|four|thirty[- ]two)"
LENGTH = rf"(?:exactly {NUMBER}|{NUMBER}\s*(?:-|to|through)\s*{NUMBER}) characters"
DUPLICATE_POLICY = r"(?:return 409 on collisions|duplicate aliases return 409)"
ALIAS_REQUESTS = (
    rf"use optional custom aliases, (?P<length>{LENGTH}); {DUPLICATE_POLICY}",
    rf"change custom aliases to (?P<length>{LENGTH}); {DUPLICATE_POLICY}",
    rf"change the approved alias acceptance rule to (?P<length>{LENGTH}); retain duplicate 409 and random defaults",
)


def _statement(text):
    """Consume one complete supported statement, including its collision policy."""
    normalized = re.sub(r"\s+", " ", text.strip().lower().replace("–", "-").replace("—", "-"))
    if any(re.fullmatch(pattern + r"\.?", normalized) for pattern in CORE_REQUESTS):
        return {"goal": "core", "bounds": None, "questions": []}
    if re.fullmatch(EXPIRATION_REQUEST + r"\.?", normalized):
        return {"goal": "expiration", "bounds": None, "questions": []}
    if re.fullmatch(AMBIGUOUS_REQUEST + r"\.?", normalized):
        return {"goal": None, "bounds": None, "questions": []}
    for pattern in ALIAS_REQUESTS:
        match = re.fullmatch(pattern + r"\.?", normalized)
        if match is None:
            continue
        length = match["length"]
        exact = re.fullmatch(rf"exactly (?P<value>{NUMBER}) characters", length)
        if exact:
            values = [exact["value"], exact["value"]]
        else:
            limits = re.fullmatch(rf"(?P<low>{NUMBER})\s*(?:-|to|through)\s*(?P<high>{NUMBER}) characters", length)
            values = [limits["low"], limits["high"]]
        cardinal = {"four": 4, "eight": 8, "thirty-two": 32, "thirty two": 32}
        bounds = tuple(cardinal[value] if value in cardinal else int(value) for value in values)
        if not 4 <= bounds[0] <= bounds[1] <= 32:
            return {"goal": "aliases", "bounds": None,
                    "questions": ["Alias bounds must satisfy 4 <= minimum <= maximum <= 32."]}
        return {"goal": "aliases", "bounds": bounds, "questions": []}
    return {"goal": None, "bounds": None, "questions": [
        "The full statement is outside the supported grammar; no clauses or constraints were ignored. "
        "Use the documented core/expiration request or 'Use optional custom aliases, exactly 8 characters; "
        "duplicate aliases return 409.'. Unsupported original requirements need a new run."]}


def interpret(context):
    """Parse a small complete-clause grammar, not arbitrary English keywords.

    Accepted forms are CORE_REQUESTS, EXPIRATION_REQUEST, AMBIGUOUS_REQUEST,
    and ALIAS_REQUESTS. Alias lengths are 'exactly N characters' or 'N-M
    characters' (also 'to'/'through'); N/M are digits or four/eight/thirty-two.
    Case, whitespace, a final period, and typographic range dashes may vary.
    The original request and latest answer must each be fully supported. A new
    complete alias answer supersedes earlier alias bounds, never unsupported
    original constraints. Duplicate HTTP 409 policy must remain explicit.
    """
    requirement = context["requirement"].strip()
    answer = context.get("answers", "").strip()
    original = _statement(requirement)
    latest = _statement(answer) if answer else None
    statements = [original] + ([latest] if latest else [])
    questions = [question for statement in statements for question in statement["questions"]]
    selected = latest or original
    goal, bounds = selected["goal"], selected["bounds"]
    expected = {"greenfield": "core", "brownfield": "expiration", "ambiguous": "aliases"}[context["scenario"]]
    goals = {statement["goal"] for statement in statements if statement["goal"] is not None}
    if goals - {expected}:
        questions.append(f"This scenario supports {expected}; use the matching scenario for the supplied intent.")
    if not questions and goal is None:
        questions.append("Should easier sharing mean optional custom aliases? Specify length bounds and duplicate HTTP 409 behavior.")
    return {"goal": goal, "bounds": bounds, "questions": questions[:3],
            "text": requirement, "answer": answer}


def _operations(inventory, goal, bounds):
    if goal == "core":
        return ["verify_existing"] if inventory["core"] else ["core"]
    if not inventory["core"]:
        raise DomainError("An additive change requires an implemented, inspected core baseline")
    if goal == "expiration":
        if inventory["expiration"]:
            return ["verify_existing"]
        return ([] if inventory["expiration_schema"] else ["expiration_schema"]) + ["expiration"]
    if goal == "aliases":
        if inventory["aliases"] and (inventory["alias_min"], inventory["alias_max"]) == bounds:
            return ["verify_existing"]
        return ["aliases"]
    raise DomainError("No supported implementation intent")


def _supplementary_test(goal, bounds, inventory):
    prefix = '''"""Rule-authored contract test selected from the interpreted requirement."""
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from shortener.app import create_app

'''
    checks = []
    if goal == "core":
        feature_checks = "\n".join(
            f'        assert ("{field}" in fields) is {bool(inventory[feature])}'
            for field, feature in (("expires_at", "expiration"), ("alias", "aliases"))
        )
        checks.append('''def test_rule_core_preserves_baseline_features(tmp_path):
    with TestClient(create_app(tmp_path / "core.db")) as client:
        schema = client.get("/openapi.json").json()
        fields = schema["components"]["schemas"]["LinkRequest"]["properties"]
FEATURE_CHECKS
        created = client.post("/api/links", json={"url": "https://example.org/core"})
        assert created.status_code == 201
        assert client.get("/r/" + created.json()["code"], follow_redirects=False).status_code == 302
'''.replace("FEATURE_CHECKS", feature_checks))
    if goal == "expiration" or inventory["expiration"]:
        checks.append('''def test_rule_expiration_addition(tmp_path):
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    with TestClient(create_app(tmp_path / "expiry.db", clock=lambda: now[0])) as client:
        expiry = now[0] + timedelta(seconds=2)
        created = client.post("/api/links", json={"url": "https://example.org/expiry", "expires_at": expiry.isoformat()})
        assert created.status_code == 201
        code = created.json()["code"]
        now[0] = expiry
        assert client.get("/r/" + code, follow_redirects=False).status_code == 410
        assert client.get("/api/links/" + code + "/stats").json()["redirect_count"] == 0
''')
    if goal == "aliases" or inventory["aliases"]:
        low, high = bounds if goal == "aliases" else (inventory["alias_min"], inventory["alias_max"])
        checks.append(f'''def test_rule_clarified_alias_bounds(tmp_path):
    with TestClient(create_app(tmp_path / "aliases.db")) as client:
        for length in sorted({{{low}, {high}}}):
            alias = "a" * length
            assert client.post("/api/links", json={{"url": "https://example.org/alias", "alias": alias}}).status_code == 201
            assert client.post("/api/links", json={{"url": "https://example.org/alias", "alias": alias}}).status_code == 409
        for length in ({low - 1}, {high + 1}):
            assert client.post("/api/links", json={{"url": "https://example.org/alias", "alias": "z" * length}}).status_code == 422
''')
    return prefix + "\n".join(checks)


class LocalProvider:
    mode = "local"
    model = "domain-rules-v1"

    def __init__(self):
        root = Path(__file__).resolve().parents[1]
        paths = [root / "orchestrator/local_provider.py", root / "orchestrator/domain.py"]
        paths += sorted((root / "orchestrator/templates").rglob("*.txt"))
        self.rule_sources = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in paths}

    def preflight(self):
        return {"provider": "local_rules", "model": self.model, "model_calls": 0,
                "notice": "Bounded requirement rules and source transformations; no API or LLM inference"}

    async def respond(self, role, context, schema):
        await asyncio.sleep(0)
        parsed = interpret(context)
        goal, bounds, questions = parsed["goal"], parsed["bounds"], parsed["questions"]
        if schema is Requirement:
            output = Requirement(
                intent=f"Supported intent: {goal or 'unresolved'}. Requirement: {parsed['text']}. Answer: {parsed['answer']}",
                acceptance=[context["contract"]] + ([f"Human alias bounds: {bounds[0]}-{bounds[1]}; duplicate HTTP 409."] if bounds else []),
                assumptions=["Deterministic domain rules; supported English vocabulary and known source structure only."],
                questions=questions, alias_min=bounds[0] if bounds else None, alias_max=bounds[1] if bounds else None,
            )
        else:
            if questions:
                raise DomainError("Unresolved requirement cannot proceed: " + " ".join(questions))
            files = context.get("files", {})
            inventory = inspect_source(files) if schema is not Documentation else None
            if schema is Plan:
                operations = _operations(inventory, goal, bounds)
                tasks = []
                for operation in operations:
                    tasks.append({"id": "task_" + operation, "kind": "implementation",
                                  "description": self._description(operation, bounds),
                                  "dependencies": [tasks[-1]["id"]] if tasks else []})
                tasks.append({"id": "task_contract_tests", "kind": "tests",
                              "description": f"Check the interpreted {goal} contract" + (f" at bounds {bounds}." if bounds else "."),
                              "dependencies": []})
                output = Plan(rationale=f"Interpreted {goal}; inspected baseline: {inventory}. "
                              f"Required operations: {operations}. " + self._impact(goal),
                              impacted_paths=["service/shortener/app.py"],
                              risks=["Only known source structure can be transformed; unknown edits stop for review.",
                                     "Reusable authored code fragments are used; no general code synthesis is claimed."], tasks=tasks)
            elif schema is ChangeSet:
                if role == "builder":
                    task = context.get("task")
                    if not isinstance(task, dict) or not task.get("id", "").startswith("task_"):
                        raise DomainError("No safe automatic rule for this failed implementation; retain evidence for human review")
                    operation = task["id"].removeprefix("task_")
                    if operation == "verify_existing":
                        changed = {"service/shortener/app.py": files["service/shortener/app.py"]}
                    else:
                        changed = apply_operation(files, operation, alias_min=bounds[0] if bounds else 4,
                                                  alias_max=bounds[1] if bounds else 32)
                else:
                    changed = {"generated_tests/test_rule_contract.py": _supplementary_test(goal, bounds, inventory)}
                output = ChangeSet(rationale=f"Rule-driven {role} output for {goal}; inspect the recorded before/after contents.", changes=[
                    {"path": path, "previous_hash": hashlib.sha256(files[path].encode()).hexdigest() if path in files else None,
                     "content": content} for path, content in changed.items()
                ])
            elif schema is Review:
                complete = inventory["core"] and (goal == "core" or inventory[goal])
                if goal == "aliases":
                    complete = complete and (inventory["alias_min"], inventory["alias_max"]) == bounds
                output = Review(summary=f"Structural rule review for {goal}; real protected tests remain authoritative.",
                                findings=[] if complete else [{"severity": "blocking", "path": "service/shortener/app.py",
                                    "evidence": str(inventory), "recommendation": "Requested capability or bounds are absent."}])
            elif schema is Documentation:
                output = Documentation(readme=f"# Rule-built URL shortener\n\nRequirement: {parsed['text']}\n\n"
                                       f"Clarification: {parsed['answer'] or 'None needed'}\n\n"
                                       f"Implemented capability: {goal}; alias bounds: {bounds}.\n\n"
                                       "Use shortener.app.create_app with a SQLite path. The orchestrator retains validation and approval evidence.\n",
                                       rationale=self._impact(goal) + f" Recorded validation: {context['validation']['summary']}.",
                                       limitations=["No API or runtime LLM. Supported domain vocabulary and source structure only.",
                                                    "Uses authored reusable code fragments; cannot implement arbitrary requests or repair unknown defects.",
                                                    "Local demonstration; no public hosting or production reliability guarantee."])
            else:
                raise DomainError("Unsupported structured role")
        return output, {"provider": "local_rules", "model": self.model, "model_calls": 0,
                        "prompt_version": "1", "rule_sources": self.rule_sources,
                        "notice": "Deterministic domain execution, not model inference"}

    @staticmethod
    def _description(operation, bounds):
        return {"core": "Construct only the core SQLite URL creation, redirects, counters and health API.",
                "expiration_schema": "Inspect links table initialization and add an idempotent nullable expires_at migration.",
                "expiration": "Extend request validation, create/redirect/stats data flow with UTC expiration; preserve existing rows and counts.",
                "aliases": f"Add or update optional aliases with source defaults {bounds}; preserve uniqueness and random codes.",
                "verify_existing": "The requested capability already exists; retain source and revalidate without duplicate additions."}[operation]

    @staticmethod
    def _impact(goal):
        return {"core": "Create the links table and POST /api/links, GET /r/{code}, stats and health routes.",
                "expiration": "Impact: links schema gains a nullable expires_at column; creation validates/persists it, redirects check it before counting, stats expose it. Core regression and migration tests must pass.",
                "aliases": "Impact: creation accepts a bounded alias in the existing unique code namespace and returns 409 on collision. Redirect/stats schema remains compatible; boundary tests follow the human answer."}[goal]
