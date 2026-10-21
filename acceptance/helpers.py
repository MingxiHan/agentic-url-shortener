"""Protected acceptance helpers; every database has a fresh, retained path."""

import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from shortener.app import create_app


NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
TARGET = "https://example.com/synthetic/path?source=assessment"


class Clock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        return self.value


def fresh_directory():
    path = Path(os.environ.get("TEST_DATA_ROOT", "/tmp/shortener-acceptance")) / uuid4().hex
    path.mkdir(parents=True)
    return path


def client(*, clock=None, code_factory=None, db_path=None):
    path = db_path if db_path is not None else fresh_directory() / "service.sqlite"
    application = create_app(path, clock=clock or Clock(), code_factory=code_factory)
    return TestClient(application, follow_redirects=False), path


def create(test_client, **fields):
    response = test_client.post("/api/links", json={"url": TARGET, **fields})
    assert response.status_code == 201, response.text
    return response.json()["code"]
