"""Contract-only skeleton: intentionally contains no working business logic."""

from datetime import datetime
from pathlib import Path
from typing import Callable

from fastapi import FastAPI


def create_app(
    db_path: str | Path,
    clock: Callable[[], datetime] | None = None,
    code_factory: Callable[[], str] | None = None,
) -> FastAPI:
    """Implement the approved service contract with these dependency seams."""
    raise NotImplementedError("The builder must implement the approved service contract")
