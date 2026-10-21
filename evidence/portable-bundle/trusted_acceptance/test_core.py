"""Mandatory behavior shared by every approved scenario."""

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from helpers import NOW, TARGET, client, create, fresh_directory


def test_create_redirect_stats_and_health():
    test_client, _ = client()
    assert test_client.get("/health").status_code == 200
    code = create(test_client)
    assert re.fullmatch(r"[a-zA-Z0-9]{8}", code)
    initial = test_client.get(f"/api/links/{code}/stats")
    assert initial.status_code == 200
    assert initial.json()["code"] == code
    assert initial.json()["redirect_count"] == 0
    assert datetime.fromisoformat(initial.json()["created_at"].replace("Z", "+00:00")) == NOW
    for _ in range(2):
        response = test_client.get(f"/r/{code}")
        assert response.status_code == 302
        assert response.headers["location"] == TARGET
        assert response.headers["cache-control"] == "no-store"
    assert test_client.get(f"/api/links/{code}/stats").json()["redirect_count"] == 2


def test_base_url_is_configuration_not_host(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://short.example.test")
    test_client, _ = client()
    response = test_client.post("/api/links", json={"url": TARGET}, headers={"host": "evil.test"})
    assert response.status_code == 201
    assert response.json()["short_url"] == f"https://short.example.test/r/{response.json()['code']}"


def test_invalid_urls_return_422():
    test_client, _ = client()
    values = [
        "", "example.com", "/relative", "ftp://example.com", "javascript:alert(1)",
        "http:///missing-host", "http://", "http://user:password@example.com", "http://user@example.com",
        "https://example.com/\nsecret", "https://example.com/\tsecret", "https://example.com/\x00secret",
        "https://example.com/\x7fsecret", "https://example.com/\x85secret", "https://example.com:invalid", "https://[broken",
        "https://example.com/" + "a" * 2048, None, 42, True,
    ]
    for value in values:
        assert test_client.post("/api/links", json={"url": value}).status_code == 422, repr(value)
    assert test_client.post("/api/links", json={}).status_code == 422


def test_valid_url_boundaries():
    test_client, _ = client()
    prefix = "https://example.com/"
    for url in ["http://example.com", "https://example.com:8443/a?b=c#fragment", prefix + "a" * (2048 - len(prefix))]:
        response = test_client.post("/api/links", json={"url": url})
        assert response.status_code == 201, response.text
        assert test_client.get(f"/r/{response.json()['code']}").status_code == 302


def test_missing_links_return_404():
    test_client, _ = client()
    assert test_client.get("/r/not-found").status_code == 404
    assert test_client.get("/api/links/not-found/stats").status_code == 404


def test_collision_attempt_limit_and_recovery():
    issued = []

    def always_same():
        issued.append("aaaaaaaa")
        return issued[-1]

    test_client, _ = client(code_factory=always_same)
    assert create(test_client) == "aaaaaaaa"
    response = test_client.post("/api/links", json={"url": TARGET})
    assert response.status_code == 503
    assert response.json()["detail"] == "code_generation_exhausted"
    assert len(issued) == 4, "One successful creation followed by exactly three collision attempts"
    sequence = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    recovery_client, _ = client(code_factory=lambda: next(sequence))
    assert create(recovery_client) == "aaaaaaaa"
    assert create(recovery_client) == "bbbbbbbb"


def test_concurrent_redirects_increment_atomically():
    test_client, _ = client()
    code = create(test_client)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: test_client.get(f"/r/{code}"), range(32)))
    assert all(response.status_code == 302 for response in responses)
    assert test_client.get(f"/api/links/{code}/stats").json()["redirect_count"] == 32


def test_database_unavailable_returns_503():
    test_client, _ = client(db_path=fresh_directory())
    assert test_client.post("/api/links", json={"url": TARGET}).status_code == 503
    assert test_client.get("/r/aaaaaaaa").status_code == 503
    assert test_client.get("/api/links/aaaaaaaa/stats").status_code == 503


def test_persists_across_app_instances():
    first, path = client()
    code = create(first)
    assert first.get(f"/r/{code}").status_code == 302
    second, _ = client(db_path=path)
    assert second.get(f"/api/links/{code}/stats").json()["redirect_count"] == 1
    assert second.get(f"/r/{code}").headers["location"] == TARGET


def test_failed_count_write_does_not_report_success():
    test_client, path = client()
    code = create(test_client)
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        assert test_client.get(f"/r/{code}").status_code == 503
    finally:
        connection.rollback()
        connection.close()
    assert test_client.get(f"/api/links/{code}/stats").json()["redirect_count"] == 0
