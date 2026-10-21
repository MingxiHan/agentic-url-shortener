"""Complete-clause parsing must never silently discard requested behavior."""

import pytest

from orchestrator.contracts import SCENARIOS
from orchestrator.local_provider import interpret


def parse(requirement, scenario="ambiguous", answer=""):
    return interpret({"requirement": requirement, "scenario": scenario, "answers": answer})


@pytest.mark.parametrize("scenario,request_text,goal", [
    ("greenfield", SCENARIOS["greenfield"], "core"),
    ("greenfield", "Create a core URL shortener with redirects and counts.", "core"),
    ("brownfield", SCENARIOS["brownfield"], "expiration"),
])
def test_complete_core_and_expiration_requests_are_supported(scenario, request_text, goal):
    result = parse(request_text, scenario)
    assert result["goal"] == goal and result["bounds"] is None and not result["questions"]


@pytest.mark.parametrize("request_text,bounds", [
    ("Use optional custom aliases, exactly eight characters; return 409 on collisions.", (8, 8)),
    ("Use optional custom aliases, exactly 8 characters; duplicate aliases return 409.", (8, 8)),
    ("Change custom aliases to 4-32 characters; duplicate aliases return 409.", (4, 32)),
    ("Change the approved alias acceptance rule to 4-32 characters; retain duplicate 409 and random defaults.", (4, 32)),
    ("Use optional custom aliases, four-thirty-two characters; return 409 on collisions.", (4, 32)),
    ("Use optional custom aliases, four to thirty two characters; duplicate aliases return 409.", (4, 32)),
    ("Use optional custom aliases, 5 through 20 characters; return 409 on collisions.", (5, 20)),
    ("Use optional custom aliases, exactly thirty-two characters; duplicate aliases return 409.", (32, 32)),
    ("Use optional custom aliases, exactly four characters; duplicate aliases return 409.", (4, 4)),
    ("  USE optional custom aliases,\n 4–32 characters; duplicate aliases return 409.  ", (4, 32)),
])
def test_explicit_alias_grammar_extracts_all_numeric_slots(request_text, bounds):
    direct = parse(request_text)
    clarified = parse(SCENARIOS["ambiguous"], answer=request_text)
    for result in (direct, clarified):
        assert result["goal"] == "aliases" and result["bounds"] == bounds
        assert not result["questions"]


def test_ambiguous_initial_request_requires_a_real_clarification():
    result = parse(SCENARIOS["ambiguous"])
    assert result["goal"] is None and result["bounds"] is None and result["questions"]


@pytest.mark.parametrize("request_text,scenario", [
    ("Build a URL shortener with four character random codes.", "greenfield"),
    ("Create a core URL shortener with redirects and counts; use four character random codes.", "greenfield"),
    ("Create a core URL shortener with redirects and counts. 还要增加登录功能", "greenfield"),
    ("Build a URL shortener with creation, redirects, aggregate counts and health; allow aliases.", "greenfield"),
    ("Add optional link expiration while preserving existing links and counts; default expiry is four days.", "brownfield"),
    ("Use optional custom aliases, exactly eight characters; duplicate aliases return 409; no-hyphen aliases.", "ambiguous"),
    ("Use optional custom aliases, exactly eight characters; duplicate aliases return 409; allow only lowercase alphanumeric characters.", "ambiguous"),
    ("Use optional custom aliases, exactly eight characters.", "ambiguous"),
    ("Change custom aliases to 4-32 characters; retain random defaults.", "ambiguous"),
    ("Use optional custom aliases, exactly eight characters; duplicate aliases return 200.", "ambiguous"),
    ("Use optional custom aliases, exactly eight characters and 4-32 characters; duplicate aliases return 409.", "ambiguous"),
    ("Use optional custom aliases, exactly eight characters; duplicate aliases return 409. 还要增加登录功能", "ambiguous"),
    ("Use optional custom aliases, exactly eight characters; duplicate aliases return 409.\x00", "ambiguous"),
])
def test_supported_vocabulary_does_not_hide_unsupported_clauses(request_text, scenario):
    assert parse(request_text, scenario)["questions"]


@pytest.mark.parametrize("length", ["3-32", "4-33", "8-4", "0-0", "exactly 99"])
def test_alias_bounds_outside_the_contract_are_refused(length):
    result = parse(f"Use optional custom aliases, {length} characters; duplicate aliases return 409.")
    assert result["questions"] and result["bounds"] is None


def test_latest_complete_alias_answer_supersedes_old_supported_bounds():
    original = "Use optional custom aliases, exactly eight characters; duplicate aliases return 409."
    answer = "Change custom aliases to four-thirty-two characters; duplicate aliases return 409."
    result = parse(original, answer=answer)
    assert not result["questions"] and result["bounds"] == (4, 32)
    assert result["text"] == original and result["answer"] == answer


@pytest.mark.parametrize("unsupported", [
    "Use optional custom aliases, exactly eight characters; duplicate aliases return 409; no-hyphen aliases.",
    "Make links easier to share. 还要增加登录功能",
    "Build a URL shortener with four character random codes.",
])
def test_valid_answer_cannot_erase_unsupported_original_constraints(unsupported):
    answer = "Use optional custom aliases, exactly eight characters; duplicate aliases return 409."
    assert parse(unsupported, answer=answer)["questions"]


def test_old_collision_policy_cannot_fill_an_incomplete_new_answer():
    original = "Use optional custom aliases, exactly eight characters; duplicate aliases return 409."
    assert parse(original, answer="Change custom aliases to 4-32 characters.")["questions"]


def test_supported_request_still_requires_the_matching_scenario():
    assert parse(SCENARIOS["brownfield"], "greenfield")["questions"]
