"""Additional mandatory behavior for the approved custom-alias scenario."""

import os
import re

import pytest

from helpers import TARGET, client, create


pytestmark = pytest.mark.skipif(os.environ.get("SCENARIO") != "ambiguous", reason="Alias scope not selected")
MINIMUM = int(os.environ.get("ALIAS_MIN_LENGTH", "4"))
MAXIMUM = int(os.environ.get("ALIAS_MAX_LENGTH", "32"))


def test_custom_alias_and_omitted_random_default():
    test_client, _ = client()
    alias = "a" * MINIMUM
    assert create(test_client, alias=alias) == alias
    response = test_client.get(f"/r/{alias}")
    assert response.status_code == 302
    assert response.headers["location"] == TARGET
    assert re.fullmatch(r"[a-zA-Z0-9]{8}", create(test_client))


def test_alias_boundaries_and_allowed_characters():
    test_client, _ = client()
    for alias in ["b" * MINIMUM, "c" * MAXIMUM, ("a-1" + "d" * MAXIMUM)[:MINIMUM]]:
        assert create(test_client, alias=alias) == alias
    for alias in ["d" * (MINIMUM - 1), "e" * (MAXIMUM + 1), "ABCD" + "a" * MINIMUM, "a_b-" + "a" * MINIMUM, "a/b-" + "a" * MINIMUM, "", 42]:
        assert test_client.post("/api/links", json={"url": TARGET, "alias": alias}).status_code == 422


def test_duplicate_and_reserved_aliases():
    test_client, _ = client()
    alias = "z" * MINIMUM
    assert create(test_client, alias=alias) == alias
    duplicate = test_client.post("/api/links", json={"url": "https://example.org/other", "alias": alias})
    assert duplicate.status_code == 409
    assert test_client.get(f"/r/{alias}").headers["location"] == TARGET
    for reserved in ["api", "r", "health", "stats", "admin", "docs", "redoc", "openapi"]:
        assert test_client.post("/api/links", json={"url": TARGET, "alias": reserved}).status_code == 422
