"""Additional mandatory behavior for the approved expiration scenario."""

import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from helpers import Clock, NOW, TARGET, client, create, fresh_directory


pytestmark = pytest.mark.skipif(os.environ.get("SCENARIO") != "brownfield", reason="Expiration scope not selected")


def test_expiration_boundary_preserves_count_and_stats():
    clock = Clock()
    test_client, _ = client(clock=clock)
    expires = NOW + timedelta(seconds=1)
    code = create(test_client, expires_at=expires.isoformat())
    assert test_client.get(f"/r/{code}").status_code == 302
    clock.value = expires
    assert test_client.get(f"/r/{code}").status_code == 410
    clock.value += timedelta(seconds=1)
    assert test_client.get(f"/r/{code}").status_code == 410
    stats = test_client.get(f"/api/links/{code}/stats")
    assert stats.status_code == 200
    assert stats.json()["redirect_count"] == 1
    assert datetime.fromisoformat(stats.json()["expires_at"].replace("Z", "+00:00")) == expires


def test_invalid_expiration_returns_422():
    test_client, _ = client()
    for value in [NOW.isoformat(), (NOW - timedelta(seconds=1)).isoformat(), "yesterday", "2031-01-01", "2031-01-01T00:00:00", 2000000000]:
        assert test_client.post("/api/links", json={"url": TARGET, "expires_at": value}).status_code == 422


def test_unexpired_default_and_nullable_expiration():
    clock = Clock()
    test_client, _ = client(clock=clock)
    for fields in [{}, {"expires_at": None}]:
        code = create(test_client, **fields)
        clock.value = NOW + timedelta(days=3650)
        assert test_client.get(f"/r/{code}").status_code == 302
        assert test_client.get(f"/api/links/{code}/stats").json()["expires_at"] is None


def test_additive_migration_preserves_existing_rows():
    path = fresh_directory() / "baseline.sqlite"
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("CREATE TABLE links (code TEXT PRIMARY KEY, target_url TEXT NOT NULL, created_at TEXT NOT NULL, redirect_count INTEGER NOT NULL DEFAULT 0)")
        connection.execute("INSERT INTO links VALUES (?,?,?,?)", ("oldcode1", TARGET, NOW.isoformat(), 7))
    connection.close()
    test_client, _ = client(db_path=path)
    stats = test_client.get("/api/links/oldcode1/stats")
    assert stats.status_code == 200
    assert stats.json()["redirect_count"] == 7
    assert stats.json()["expires_at"] is None
    assert test_client.get("/r/oldcode1").headers["location"] == TARGET
    restarted, _ = client(db_path=path)
    assert restarted.get("/api/links/oldcode1/stats").json()["redirect_count"] == 8
