"""Rule-authored contract test selected from the interpreted requirement."""
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from shortener.app import create_app

def test_rule_expiration_addition(tmp_path):
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    with TestClient(create_app(tmp_path / "expiry.db", clock=lambda: now[0])) as client:
        expiry = now[0] + timedelta(seconds=2)
        created = client.post("/api/links", json={"url": "https://example.org/expiry", "expires_at": expiry.isoformat()})
        assert created.status_code == 201
        code = created.json()["code"]
        now[0] = expiry
        assert client.get("/r/" + code, follow_redirects=False).status_code == 410
        assert client.get("/api/links/" + code + "/stats").json()["redirect_count"] == 0
