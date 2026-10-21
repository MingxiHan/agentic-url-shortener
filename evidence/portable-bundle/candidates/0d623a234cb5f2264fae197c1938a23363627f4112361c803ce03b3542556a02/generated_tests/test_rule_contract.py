"""Rule-authored contract test selected from the interpreted requirement."""
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from shortener.app import create_app

def test_rule_clarified_alias_bounds(tmp_path):
    with TestClient(create_app(tmp_path / "aliases.db")) as client:
        for length in sorted({4, 32}):
            alias = "a" * length
            assert client.post("/api/links", json={"url": "https://example.org/alias", "alias": alias}).status_code == 201
            assert client.post("/api/links", json={"url": "https://example.org/alias", "alias": alias}).status_code == 409
        for length in (3, 33):
            assert client.post("/api/links", json={"url": "https://example.org/alias", "alias": "z" * length}).status_code == 422
