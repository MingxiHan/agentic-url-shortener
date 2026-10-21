"""Bounded, rule-built source transformations for the URL-shortener assessment.

This module parses source without importing it. It recognizes the contract-only
skeleton and authored core, expiration, and alias variants, then applies small
explicit edits. Comments and unrelated top-level helpers remain in place. Changes
to owned definitions or ambiguous edit anchors require human review. This is a
domain-specific compiler using reusable human/AI-authored fragments, not general
codebase reasoning or LLM synthesis; it never reads the all-feature reference.
"""

import ast
from pathlib import Path


APP_PATH = "service/shortener/app.py"
TEMPLATE = Path(__file__).with_name("templates") / "core.py.txt"


class DomainError(ValueError):
    """The source or requested transformation is outside the supported contract."""


SCHEMA_ANCHOR = '''                    "created_at TEXT NOT NULL, redirect_count INTEGER NOT NULL DEFAULT 0)"
                )'''
SCHEMA_ADDITION = '''
                columns = {row[1] for row in connection.execute("PRAGMA table_info(links)")}
                if "expires_at" not in columns:
                    connection.execute("ALTER TABLE links ADD COLUMN expires_at TEXT")'''
EXPIRATION_VALIDATORS = '''

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
'''
EXPIRATION_CHECK = '''        if request.expires_at is not None and request.expires_at <= now:
            raise HTTPException(status_code=422, detail="expiration_must_be_in_future")
'''
REDIRECT_ANCHOR = '''                connection.execute(
                    "UPDATE links SET redirect_count=redirect_count+1 WHERE code=?", (code,)
                )'''
EXPIRATION_REDIRECT = '''                if row["expires_at"] is not None:
                    expires_at = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
                    if clock() >= expires_at:
                        raise HTTPException(status_code=410, detail="link_expired")
'''
RESERVED = '''RESERVED_ALIASES = frozenset(
    {"api", "r", "health", "stats", "admin", "docs", "redoc", "openapi"}
)
'''
ALIAS_CHECK = '''        if request.alias is not None:
            if (
                not alias_min <= len(request.alias) <= alias_max
                or re.fullmatch(r"[a-z0-9-]+", request.alias) is None
                or request.alias in RESERVED_ALIASES
            ):
                raise HTTPException(status_code=422, detail="invalid_alias")
'''
CREATE_ANCHOR = '''        now = clock()
'''
APP_ANCHOR = '''    app = FastAPI(title="Rule-Built URL Shortener", version="0.1.0")'''


def _replace(source, before, after):
    if source.count(before) != 1:
        raise DomainError("Source edit anchor is absent or ambiguous; manual review is required")
    return source.replace(before, after, 1)


def _bounds(alias_min, alias_max):
    if type(alias_min) is not int or type(alias_max) is not int or not 4 <= alias_min <= alias_max <= 32:
        raise DomainError("Alias bounds must satisfy 4 <= minimum <= maximum <= 32")


def _alias_settings(alias_min, alias_max):
    return (f'    alias_min = int(os.environ.get("ALIAS_MIN_LENGTH", "{alias_min}"))\n'
            f'    alias_max = int(os.environ.get("ALIAS_MAX_LENGTH", "{alias_max}"))\n')


def _transform(source, operation, alias_min=4, alias_max=32):
    if operation == "expiration_schema":
        return _replace(source, SCHEMA_ANCHOR, SCHEMA_ANCHOR + SCHEMA_ADDITION)
    if operation == "expiration":
        source = _replace(source, "    url: StrictStr\n", "    url: StrictStr\n    expires_at: datetime | None = None\n")
        request = next(node for node in _parse(source).body if isinstance(node, ast.ClassDef) and node.name == "LinkRequest")
        lines = source.splitlines(keepends=True)
        source = "".join(lines[:request.end_lineno]) + EXPIRATION_VALIDATORS + "".join(lines[request.end_lineno:])
        source = _replace(source, CREATE_ANCHOR, CREATE_ANCHOR + EXPIRATION_CHECK)
        source = _replace(source, '"INSERT INTO links(code,target_url,created_at,redirect_count) "',
                          '"INSERT INTO links(code,target_url,created_at,redirect_count,expires_at) "')
        source = _replace(source, '"VALUES (?,?,?,0)"', '"VALUES (?,?,?,0,?)"')
        source = _replace(source, "(code, request.url, timestamp(now)),",
                          "(code, request.url, timestamp(now), timestamp(request.expires_at) if request.expires_at else None),")
        source = _replace(source, REDIRECT_ANCHOR, EXPIRATION_REDIRECT + REDIRECT_ANCHOR)
        return _replace(source, "SELECT code,created_at,redirect_count FROM links", "SELECT code,created_at,redirect_count,expires_at FROM links")
    if operation == "aliases":
        source = _replace(source, "import os\n", "import os\nimport re\n")
        source = _replace(source, "ALPHABET = string.ascii_letters + string.digits\n", RESERVED + "ALPHABET = string.ascii_letters + string.digits\n")
        field_anchor = "    expires_at: datetime | None = None\n" if "    expires_at: datetime | None = None\n" in source else "    url: StrictStr\n"
        source = _replace(source, field_anchor, field_anchor + "    alias: StrictStr | None = None\n")
        source = _replace(source, APP_ANCHOR, _alias_settings(alias_min, alias_max) + APP_ANCHOR)
        check_anchor = CREATE_ANCHOR + EXPIRATION_CHECK if CREATE_ANCHOR + EXPIRATION_CHECK in source else CREATE_ANCHOR
        source = _replace(source, check_anchor, check_anchor + ALIAS_CHECK)
        source = _replace(source, "            for _ in range(3):\n                code = code_factory()",
                          "            for _ in range(1 if request.alias is not None else 3):\n                code = request.alias if request.alias is not None else code_factory()")
        return _replace(source, "                except sqlite3.IntegrityError:\n",
                        "                except sqlite3.IntegrityError:\n                    if request.alias is not None:\n                        raise HTTPException(status_code=409, detail=\"alias_already_exists\")\n")
    raise DomainError(f"Unsupported operation: {operation}")


def _parse(source):
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError, TypeError) as error:
        raise DomainError("Source is not valid Python; manual review is required") from error


def _dump(node):
    return ast.dump(node, include_attributes=False)


def _bindings(tree):
    """Inspect top-level names; reject executable structure outside this compiler."""
    definitions, imports = {}, {}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and (node.level or any(item.name == "*" for item in node.names)):
                raise DomainError("Relative and wildcard imports need manual review")
            for item in node.names:
                name = item.asname or (item.name.split(".")[0] if isinstance(node, ast.Import) else item.name)
                origin = (None if isinstance(node, ast.Import) else node.module, item.name)
                if name in definitions or (name in imports and imports[name] != origin):
                    raise DomainError(f"Conflicting source binding: {name}")
                imports[name] = origin
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in definitions or node.name in imports:
                raise DomainError(f"Conflicting source binding: {node.name}")
            definitions[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                raise DomainError("Complex top-level assignments need manual review")
            name = targets[0].id
            if name in definitions or name in imports:
                raise DomainError(f"Conflicting source binding: {name}")
            definitions[name] = node
        elif not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            raise DomainError("Executable top-level structure needs manual review")
    return definitions, imports


def _known_definitions(source, expected):
    actual_defs, actual_imports = _bindings(_parse(source))
    expected_defs, expected_imports = _bindings(_parse(expected))
    for name, node in expected_defs.items():
        if name not in actual_defs or _dump(actual_defs[name]) != _dump(node):
            raise DomainError(f"Unsupported changes to owned definition {name}; manual review is required")
    for name, origin in expected_imports.items():
        if actual_imports.get(name) != origin:
            raise DomainError(f"Required import differs: {name}")
    return actual_defs


def _skeleton(source):
    definitions, imports = _bindings(_parse(source))
    function = definitions.get("create_app")
    template_function = next(node for node in _parse(TEMPLATE.read_text()).body
                             if isinstance(node, ast.FunctionDef) and node.name == "create_app")
    if not isinstance(function, ast.FunctionDef) or _dump(function.args) != _dump(template_function.args):
        return False
    body = function.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if (function.decorator_list or len(body) != 1 or not isinstance(body[0], ast.Raise)
            or not isinstance(body[0].exc, ast.Call) or not isinstance(body[0].exc.func, ast.Name)
            or body[0].exc.func.id != "NotImplementedError"):
        return False
    owned, required = _bindings(_parse(TEMPLATE.read_text()))
    if any(name != "create_app" and name in definitions for name in owned):
        raise DomainError("Skeleton conflicts with an owned service definition")
    if any(name in imports and imports[name] != origin for name, origin in required.items()):
        raise DomainError("Skeleton has a conflicting service import")
    return True


def inspect_source(files: dict[str, str]) -> dict:
    """Return verified feature inventory and concrete impact observations, never execute source."""
    source = files.get(APP_PATH)
    if not isinstance(source, str):
        raise DomainError(f"Missing supported service source: {APP_PATH}")
    tree = _parse(source)
    if _skeleton(source):
        return {"core": False, "expiration_schema": False, "expiration": False, "aliases": False,
                "alias_min": None, "alias_max": None, "impacted_paths": [APP_PATH],
                "observations": ["create_app contains the contract-only NotImplementedError skeleton."]}
    definitions, _ = _bindings(tree)
    request = definitions.get("LinkRequest")
    if not isinstance(request, ast.ClassDef):
        raise DomainError("Unsupported baseline: LinkRequest is absent")
    fields = {node.target.id for node in request.body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}
    expiration, aliases = "expires_at" in fields, "alias" in fields
    schema = any(isinstance(node, ast.Constant) and node.value == "ALTER TABLE links ADD COLUMN expires_at TEXT" for node in ast.walk(tree))
    if expiration and not schema:
        raise DomainError("Expiration API requires the supported additive schema migration")
    bounds = {}
    function = definitions.get("create_app")
    if aliases and isinstance(function, ast.FunctionDef):
        for node in function.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in {"alias_min", "alias_max"}:
                try:
                    bounds[node.targets[0].id] = int(node.value.args[0].args[1].value)
                except (AttributeError, IndexError, TypeError, ValueError) as error:
                    raise DomainError("Alias defaults must be supported integer literals") from error
        if set(bounds) != {"alias_min", "alias_max"}:
            raise DomainError("Alias defaults are missing")
        _bounds(**bounds)
    expected = TEMPLATE.read_text()
    if schema:
        expected = _transform(expected, "expiration_schema")
    if expiration:
        expected = _transform(expected, "expiration")
    if aliases:
        expected = _transform(expected, "aliases", **bounds)
    _known_definitions(source, expected)
    observations = ["LinkRequest and create_app match the supported core API and SQLite behavior.",
                    "Core routes: POST /api/links, GET /r/{code}, GET /api/links/{code}/stats, GET /health.",
                    "Expiration touches LinkRequest, links schema migration, creation, redirect and statistics." if schema or expiration
                    else "No expiration field, migration, or redirect expiration check is present.",
                    "Aliases reuse links.code and affect validation and collision handling." if aliases
                    else "Creation uses random codes; no custom-alias request field is present."]
    return {"core": True, "expiration_schema": schema, "expiration": expiration, "aliases": aliases,
            "alias_min": bounds.get("alias_min"), "alias_max": bounds.get("alias_max"),
            "impacted_paths": [APP_PATH], "observations": observations}


def apply_operation(files: dict[str, str], operation: str, alias_min=4, alias_max=32) -> dict[str, str]:
    """Return only changed source files; refuse incompatible or out-of-order operations."""
    if operation not in {"core", "expiration_schema", "expiration", "aliases"}:
        raise DomainError(f"Unsupported operation: {operation}")
    inventory = inspect_source(files)
    source = files[APP_PATH]
    if operation == "core":
        if inventory["core"]:
            return {}
        tree = _parse(source)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "create_app")
        lines = source.splitlines(keepends=True)
        # Replace only the approved stub; retain surrounding source and comments.
        source = "".join(lines[:function.lineno - 1]) + TEMPLATE.read_text() + "".join(lines[function.end_lineno:])
        if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str):
            original = ast.get_source_segment(files[APP_PATH], tree.body[0])
            source = source.replace(original, '"""Rule-built URL shortener; composed from supported authored fragments."""', 1)
    else:
        if not inventory["core"]:
            raise DomainError("The core operation must precede feature additions")
        if operation == "aliases":
            _bounds(alias_min, alias_max)
            if inventory["aliases"]:
                source = _replace(source, _alias_settings(inventory["alias_min"], inventory["alias_max"]), _alias_settings(alias_min, alias_max))
            else:
                source = _transform(source, operation, alias_min, alias_max)
        elif inventory[operation]:
            return {}
        elif operation == "expiration" and not inventory["expiration_schema"]:
            raise DomainError("The expiration_schema operation must precede expiration API changes")
        else:
            source = _transform(source, operation)
    inspect_source({**files, APP_PATH: source})
    return {APP_PATH: source} if source != files[APP_PATH] else {}
