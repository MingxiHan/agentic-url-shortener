"""Behavior and preservation checks for the bounded, rule-built source compiler."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import types

from fastapi.testclient import TestClient
import pytest

from orchestrator.domain import APP_PATH, DomainError, apply_operation, inspect_source


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
TARGET = "https://example.com/path"


def skeleton():
    return {f"service/shortener/{path.name}": path.read_text() for path in (ROOT / "skeleton" / "shortener").glob("*.py")}


def applied(files, operation, **kwargs):
    return {**files, **apply_operation(files, operation, **kwargs)}


def application(files, path, **kwargs):
    # Execute only these authored compiler outputs, never external candidate source.
    module = types.ModuleType("rule_built_test_service")
    exec(compile(files[APP_PATH], APP_PATH, "exec"), module.__dict__)
    return module.create_app(path, **kwargs)


def test_core_is_built_from_stub_without_unrequested_features(tmp_path):
    files = skeleton()
    assert not inspect_source(files)["core"]
    changes = apply_operation(files, "core")
    assert set(changes) == {APP_PATH}
    files.update(changes)
    inventory = inspect_source(files)
    assert inventory["core"]
    assert not any(inventory[key] for key in ("expiration_schema", "expiration", "aliases"))
    assert "expires_at" not in files[APP_PATH] and "alias" not in files[APP_PATH]
    assert "Rule-built" in files[APP_PATH]
    path = tmp_path / "core.sqlite"
    with TestClient(application(files, path, clock=lambda: NOW), follow_redirects=False) as client:
        result = client.post("/api/links", json={"url": TARGET})
        assert result.status_code == 201
        code = result.json()["code"]
        assert client.get(f"/r/{code}").status_code == 302
        assert client.get(f"/api/links/{code}/stats").json()["redirect_count"] == 1
        assert client.post("/api/links", json={"url": TARGET, "alias": "custom"}).status_code == 422
        assert client.post("/api/links", json={"url": TARGET, "expires_at": None}).status_code == 422
    connection = sqlite3.connect(path)
    try:
        assert "expires_at" not in {row[1] for row in connection.execute("PRAGMA table_info(links)")}
    finally:
        connection.close()
    assert apply_operation(files, "core") == {}


def test_expiration_migration_then_api_preserves_rows_and_actual_behavior(tmp_path):
    files = applied(skeleton(), "core")
    path = tmp_path / "migration.sqlite"
    with TestClient(application(files, path, clock=lambda: NOW), follow_redirects=False) as client:
        old = client.post("/api/links", json={"url": TARGET}).json()["code"]
        assert client.get(f"/r/{old}").status_code == 302
    with pytest.raises(DomainError, match="expiration_schema"):
        apply_operation(files, "expiration")
    files = applied(files, "expiration_schema")
    assert inspect_source(files)["expiration_schema"] and not inspect_source(files)["expiration"]
    with TestClient(application(files, path, clock=lambda: NOW)) as client:
        assert client.get(f"/api/links/{old}/stats").json()["redirect_count"] == 1
        assert client.post("/api/links", json={"url": TARGET, "expires_at": None}).status_code == 422
    files = applied(files, "expiration")
    assert inspect_source(files)["expiration"] and not inspect_source(files)["aliases"]
    clock = [NOW]
    with TestClient(application(files, path, clock=lambda: clock[0]), follow_redirects=False) as client:
        old_stats = client.get(f"/api/links/{old}/stats").json()
        assert old_stats["redirect_count"] == 1 and old_stats["expires_at"] is None
        expires = NOW + timedelta(seconds=1)
        created = client.post("/api/links", json={"url": TARGET, "expires_at": expires.isoformat()})
        assert created.status_code == 201
        code = created.json()["code"]
        assert client.get(f"/r/{code}").status_code == 302
        clock[0] = expires
        assert client.get(f"/r/{code}").status_code == 410
        assert client.get(f"/api/links/{code}/stats").json()["redirect_count"] == 1
        for invalid in ["2031-01-01", "2031-01-01T00:00:00", 2000000000, NOW.isoformat()]:
            assert client.post("/api/links", json={"url": TARGET, "expires_at": invalid}).status_code == 422
    assert apply_operation(files, "expiration_schema") == {}
    assert apply_operation(files, "expiration") == {}


def test_alias_bounds_change_source_and_defaults_with_environment_override(tmp_path, monkeypatch):
    monkeypatch.delenv("ALIAS_MIN_LENGTH", raising=False)
    monkeypatch.delenv("ALIAS_MAX_LENGTH", raising=False)
    files = applied(applied(skeleton(), "core"), "aliases", alias_min=8, alias_max=8)
    inventory = inspect_source(files)
    assert (inventory["alias_min"], inventory["alias_max"]) == (8, 8)
    assert not inventory["expiration"] and not inventory["expiration_schema"]
    with TestClient(application(files, tmp_path / "eight.sqlite")) as client:
        assert client.post("/api/links", json={"url": TARGET, "alias": "abcd"}).status_code == 422
        assert client.post("/api/links", json={"url": TARGET, "alias": "abcdefgh"}).status_code == 201
        assert client.post("/api/links", json={"url": TARGET, "alias": "abcdefgh"}).status_code == 409
    revised = applied(files, "aliases", alias_min=4, alias_max=32)
    assert files[APP_PATH] != revised[APP_PATH]
    assert inspect_source(revised)["alias_min"] == 4
    with TestClient(application(revised, tmp_path / "range.sqlite")) as client:
        assert client.post("/api/links", json={"url": TARGET, "alias": "abcd"}).status_code == 201
        assert client.post("/api/links", json={"url": TARGET, "alias": "x" * 32}).status_code == 201
        assert client.post("/api/links", json={"url": TARGET, "alias": "admin"}).status_code == 422
    monkeypatch.setenv("ALIAS_MIN_LENGTH", "4")
    monkeypatch.setenv("ALIAS_MAX_LENGTH", "32")
    with TestClient(application(files, tmp_path / "override.sqlite")) as client:
        assert client.post("/api/links", json={"url": TARGET, "alias": "abcd"}).status_code == 201
    assert apply_operation(revised, "aliases", alias_min=4, alias_max=32) == {}


@pytest.mark.parametrize("order", [("expiration_schema", "expiration", "aliases"), ("aliases", "expiration_schema", "expiration")])
def test_feature_operations_compose_in_both_orders(order, tmp_path):
    files = applied(skeleton(), "core")
    for operation in order:
        files = applied(files, operation)
    inventory = inspect_source(files)
    assert all(inventory[key] for key in ("core", "expiration_schema", "expiration", "aliases"))
    with TestClient(application(files, tmp_path / "composed.sqlite")) as client:
        result = client.post("/api/links", json={"url": TARGET, "alias": "my-link", "expires_at": "2031-01-01T00:00:00Z"})
        assert result.status_code == 201 and result.json()["code"] == "my-link"


def test_preserves_unrelated_source_files_helpers_and_comments():
    files = skeleton()
    files[APP_PATH] = "# Operator note before skeleton.\n" + files[APP_PATH] + '\n\ndef custom_helper():\n    return "unchanged"\n'
    files["docs/operator.md"] = "Retain this unrelated file.\n"
    files = applied(files, "core")
    extra = '\n\ndef additional_helper():\n    return "also unchanged"\n'
    files[APP_PATH] = files[APP_PATH].replace("\n\ndef create_app(\n", extra + "\n\ndef create_app(\n")
    files[APP_PATH] = files[APP_PATH].replace("        now = clock()\n", "        # Retain this service comment.\n        now = clock()\n")
    for operation in ("expiration_schema", "expiration", "aliases"):
        changes = apply_operation(files, operation)
        assert set(changes) == {APP_PATH}
        files.update(changes)
    for text in ("# Operator note before skeleton.", "# Retain this service comment.",
                 'return "unchanged"', 'return "also unchanged"'):
        assert text in files[APP_PATH]
    assert files["docs/operator.md"] == "Retain this unrelated file.\n"


@pytest.mark.parametrize("change", [
    lambda source: source.replace("REDIRECT_STATUS = 302", "REDIRECT_STATUS = 307"),
    lambda source: source.replace("for _ in range(3)", "for _ in range(30)"),
    lambda source: source.replace("def create_app(", "def renamed_app("),
    lambda source: source + "\ncreate_app = lambda *args: None\n",
    lambda source: "this is invalid Python !",
])
def test_incompatible_owned_logic_is_refused_without_wholesale_replacement(change):
    files = applied(skeleton(), "core")
    files[APP_PATH] = change(files[APP_PATH])
    before = dict(files)
    with pytest.raises(DomainError):
        apply_operation(files, "expiration_schema")
    assert files == before


def test_unsupported_operations_and_invalid_bounds_fail_closed():
    files = skeleton()
    with pytest.raises(DomainError, match="core"):
        apply_operation(files, "aliases")
    files = applied(files, "core")
    with pytest.raises(DomainError, match="Unsupported operation"):
        apply_operation(files, "payments")
    for bounds in [(3, 32), (8, 4), (4, 33), (True, 32)]:
        with pytest.raises(DomainError, match="Alias bounds"):
            apply_operation(files, "aliases", *bounds)
