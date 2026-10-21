"""Trusted scenario constraints, supplied to every model role."""

SCENARIOS = {
    "greenfield": "Build a URL shortener with creation, redirects, aggregate counts and health.",
    "brownfield": "Add optional link expiration while preserving existing links and counts.",
    "ambiguous": "Make links easier to share.",
}

CORE = """Implement shortener.app.create_app(db_path, clock=None, code_factory=None).
clock returns an aware UTC datetime. code_factory returns a string. Defaults use UTC now
and a cryptographic eight-character alphanumeric code. POST /api/links accepts {url} and
returns 201 {code, short_url}. GET /r/{code} returns 302 with Cache-Control: no-store,
increments redirect_count atomically, and returns 404 for unknown codes. GET
/api/links/{code}/stats returns code, created_at and redirect_count (404 if unknown).
GET /health is process health. BASE_URL environment defaults to http://localhost:8000;
never trust incoming Host for short_url. Use SQLite, parameterized SQL, bounded busy
timeout and atomic transactions. Three total collision insert attempts, then
503 detail=code_generation_exhausted. Database errors are 503 detail=database_unavailable,
including an unusable DB path, without crashing create_app. Reject invalid input with
422: URL length >2048, absent host, schemes other than http/https, credentials, or
control characters. Never fetch targets. All code is inside service/shortener/*.py;
no dependency installation, configuration files, tools, network access, or deletions.
Runtime dependencies are FastAPI, pydantic and Python stdlib. Tests may use pytest/httpx.
"""


def contract(scenario: str, alias_min: int = 4, alias_max: int = 32) -> str:
    if scenario not in SCENARIOS:
        raise ValueError("Unknown scenario")
    extra = ""
    if scenario == "brownfield":
        extra = """Add optional expires_at to creation and stats. Absent means permanent.
Require future ISO 8601 UTC strings with an explicit UTC timezone; numeric, naive,
non-UTC, invalid or past input returns 422. At
now >= expires_at, redirect is 410 and does not increment. Stats remain available.
Perform an idempotent additive nullable-column migration of existing links; preserve
old rows and counters. Never change the accepted baseline DB; use a candidate copy.
"""
    elif scenario == "ambiguous":
        extra = f"""After human clarification, add optional alias at creation. Aliases are
{alias_min}-{alias_max} lowercase alphanumeric or hyphen characters. Missing alias
retains random generation. Duplicate aliases return 409. Reserved names are api, r,
health, stats, admin, docs, redoc, openapi (422). Respect ALIAS_MIN_LENGTH and
ALIAS_MAX_LENGTH environment values for the accepted contract. Use the same code
namespace; DB uniqueness is authoritative. Existing endpoints remain compatible.
"""
    return CORE + extra
