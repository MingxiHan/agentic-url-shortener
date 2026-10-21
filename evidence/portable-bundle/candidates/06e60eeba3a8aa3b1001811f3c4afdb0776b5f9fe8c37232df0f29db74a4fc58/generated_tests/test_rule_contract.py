"""Rule-authored contract test selected from the interpreted requirement."""
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from shortener.app import create_app

def test_rule_core_preserves_baseline_features(tmp_path):
    with TestClient(create_app(tmp_path / "core.db")) as client:
        schema = client.get("/openapi.json").json()
        fields = schema["components"]["schemas"]["LinkRequest"]["properties"]
        assert ("expires_at" in fields) is False
        assert ("alias" in fields) is False
        created = client.post("/api/links", json={"url": "https://example.org/core"})
        assert created.status_code == 201
        assert client.get("/r/" + created.json()["code"], follow_redirects=False).status_code == 302
