"""Export the tested service's real OpenAPI and SQLite table definitions."""

import json
from pathlib import Path
import sqlite3
import sys

from fastapi.testclient import TestClient

def export(service: Path = Path("/candidate/service"), output: Path = Path("/tmp")) -> None:
    sys.path.insert(0, str(service))
    from shortener.app import create_app

    database = output / "schema.sqlite3"
    app = create_app(db_path=database)
    with TestClient(app):
        (output / "api.json").write_text(json.dumps(app.openapi(), indent=2, sort_keys=True))
        with sqlite3.connect(database) as connection:
            rows = connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
            schema = {"tables": [{"name": name, "sql": sql} for name, sql in rows]}
        (output / "schema.json").write_text(json.dumps(schema, indent=2, sort_keys=True))


if __name__ == "__main__":
    export()
