"""Rule-built URL shortener; composed from supported authored fragments."""

from datetime import datetime
from pathlib import Path
from typing import Callable

from fastapi import FastAPI


"""Rule-built URL shortener from an authored core template; no model inference."""

import os
import re
import secrets
import sqlite3
import string
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, StrictStr, field_validator


RESERVED_ALIASES = frozenset(
    {"api", "r", "health", "stats", "admin", "docs", "redoc", "openapi"}
)
ALPHABET = string.ascii_letters + string.digits
REDIRECT_STATUS = 302


class LinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: StrictStr
    expires_at: datetime | None = None
    alias: StrictStr | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("URL must contain valid Unicode") from exc
        if not value or len(value) > 2048 or any(c.isspace() or unicodedata.category(c) == "Cc" for c in value):
            raise ValueError("URL must contain 1-2048 characters without whitespace or controls")
        try:
            parts = urlsplit(value)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ValueError("An absolute HTTP(S) URL with a host is required")
            if parts.username is not None or parts.password is not None:
                raise ValueError("Credentials are not allowed in URLs")
            if "\\" in parts.netloc or "%" in parts.netloc:
                raise ValueError("Invalid host")
            _ = parts.port
        except (ValueError, UnicodeError) as exc:
            raise ValueError(str(exc)) from exc
        return value


    @field_validator("expires_at", mode="before")
    @classmethod
    def validate_expiration_type(cls, value: object) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("Expiration must be an ISO 8601 UTC string")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expiration_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset().total_seconds() != 0):
            raise ValueError("Expiration must include the UTC timezone")
        return value


def create_app(
    db_path: str | Path,
    clock: Callable[[], datetime] | None = None,
    code_factory: Callable[[], str] | None = None,
) -> FastAPI:
    """Create an independent local service with explicit dependency seams."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    code_factory = code_factory or (lambda: "".join(secrets.choice(ALPHABET) for _ in range(8)))
    base_url = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
    alias_min = int(os.environ.get("ALIAS_MIN_LENGTH", "4"))
    alias_max = int(os.environ.get("ALIAS_MAX_LENGTH", "32"))
    app = FastAPI(title="Rule-Built URL Shortener", version="0.1.0")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, error: RequestValidationError) -> JSONResponse:
        # Do not echo malformed Unicode or destination URLs in errors.
        return JSONResponse(
            status_code=422,
            content={"detail": [{key: issue[key] for key in ("type", "loc", "msg")} for issue in error.errors()]},
        )

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(str(db_path), timeout=2.0)
        connection.row_factory = sqlite3.Row
        return connection

    def timestamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    initialization_failed = False
    try:
        connection = connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS links ("
                    "code TEXT PRIMARY KEY, target_url TEXT NOT NULL, "
                    "created_at TEXT NOT NULL, redirect_count INTEGER NOT NULL DEFAULT 0)"
                )
                columns = {row[1] for row in connection.execute("PRAGMA table_info(links)")}
                if "expires_at" not in columns:
                    connection.execute("ALTER TABLE links ADD COLUMN expires_at TEXT")
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        initialization_failed = True

    def open_database() -> sqlite3.Connection:
        if initialization_failed:
            raise HTTPException(status_code=503, detail="database_unavailable")
        return connect()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/api/links", status_code=201)
    def create_link(request: LinkRequest) -> dict:
        now = clock()
        if request.expires_at is not None and request.expires_at <= now:
            raise HTTPException(status_code=422, detail="expiration_must_be_in_future")
        if request.alias is not None:
            if (
                not alias_min <= len(request.alias) <= alias_max
                or re.fullmatch(r"[a-z0-9-]+", request.alias) is None
                or request.alias in RESERVED_ALIASES
            ):
                raise HTTPException(status_code=422, detail="invalid_alias")
        connection = None
        try:
            connection = open_database()
            for _ in range(1 if request.alias is not None else 3):
                code = request.alias if request.alias is not None else code_factory()
                try:
                    with connection:
                        connection.execute(
                            "INSERT INTO links(code,target_url,created_at,redirect_count,expires_at) "
                            "VALUES (?,?,?,0,?)",
                            (code, request.url, timestamp(now), timestamp(request.expires_at) if request.expires_at else None),
                        )
                except sqlite3.IntegrityError:
                    if request.alias is not None:
                        raise HTTPException(status_code=409, detail="alias_already_exists")
                    continue
                return {"code": code, "short_url": f"{base_url}/r/{code}"}
            raise HTTPException(status_code=503, detail="code_generation_exhausted")
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503, detail="database_unavailable") from exc
        finally:
            if connection is not None:
                connection.close()

    @app.get("/r/{code}")
    def redirect(code: str) -> RedirectResponse:
        connection = None
        try:
            connection = open_database()
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM links WHERE code=?", (code,)).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="link_not_found")
                if row["expires_at"] is not None:
                    expires_at = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
                    if clock() >= expires_at:
                        raise HTTPException(status_code=410, detail="link_expired")
                connection.execute(
                    "UPDATE links SET redirect_count=redirect_count+1 WHERE code=?", (code,)
                )
            return RedirectResponse(
                row["target_url"], status_code=REDIRECT_STATUS, headers={"Cache-Control": "no-store"}
            )
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503, detail="database_unavailable") from exc
        finally:
            if connection is not None:
                connection.close()

    @app.get("/api/links/{code}/stats")
    def statistics(code: str) -> dict:
        connection = None
        try:
            connection = open_database()
            row = connection.execute(
                "SELECT code,created_at,redirect_count,expires_at FROM links WHERE code=?", (code,)
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="link_not_found")
            return dict(row)
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503, detail="database_unavailable") from exc
        finally:
            if connection is not None:
                connection.close()

    return app
