# URL shortener API

The application interface is `shortener.app.create_app(db_path, clock=None, code_factory=None)`. A caller creates the database's parent directory and provides a candidate-local SQLite path. Test seams supply an aware UTC `datetime` from `clock()` and a string from `code_factory()`; ordinary defaults use the UTC clock and eight cryptographically random alphanumeric characters. These seams are not HTTP endpoints.

## Endpoints

| Request | Successful response | Notes |
| --- | --- | --- |
| `POST /api/links` | `201`, `{"code":"Ab12Cd34","short_url":"http://localhost:8000/r/Ab12Cd34"}` | JSON requires `url`; optional features depend on the approved scenario. |
| `GET /r/{code}` | `302`, `Location` and `Cache-Control: no-store` headers | Atomically increments the stored request count. |
| `GET /api/links/{code}/stats` | `200`, `code`, `created_at`, `redirect_count` | Expiration scope additionally includes nullable `expires_at`; statistics remain readable after expiration. |
| `GET /health` | `200`, `{"status":"ok"}` | Process health, not a database readiness guarantee. |

`url` is an absolute HTTP or HTTPS URL with a host, at most 2,048 characters. Credentials, whitespace, control characters, malformed Unicode, and invalid ports are rejected. The server never fetches or previews destinations. `BASE_URL` controls published short URLs and defaults to `http://localhost:8000`; request Host headers are ignored for this purpose.

Creation example:

```json
{"url":"https://example.com/synthetic"}
```

In the expiration scenario, add `"expires_at":"2030-12-31T00:00:00Z"`. It must be a future ISO 8601 UTC string; omitted or null means no expiration. At `now >= expires_at`, redirects return `410` without incrementing counts. Initialization adds a nullable column to the supplied database and preserves old rows and counts. Acceptance tests use a fresh retained test database; the orchestrator does not copy or roll back a deployed user database.

In the custom-alias scenario, add `"alias":"my-link"`. Default approved bounds are 4–32 lowercase ASCII letters, digits, or hyphens; the initial replan demonstration uses exactly eight characters. `ALIAS_MIN_LENGTH` and `ALIAS_MAX_LENGTH` convey the approved bounds. Omission retains random codes. Aliases share the existing code namespace; duplicates do not replace existing links. Reserved names are `api`, `r`, `health`, `stats`, `admin`, `docs`, `redoc`, and `openapi`.

## Errors and consistency

| Status | Meaning |
| --- | --- |
| `404` | Unknown code. |
| `409` | Requested alias already exists. |
| `410` | Link has expired. |
| `422` | Invalid request, URL, alias, or expiration. |
| `503`, `detail: "database_unavailable"` | Database initialization, lookup, write, or commit failed; no successful redirect is claimed. |
| `503`, `detail: "code_generation_exhausted"` | All three generated-code insert attempts collided. |

Counts record accepted redirect requests, including repeats and bots. They do not measure unique visitors or completed navigation. Lookup, expiration decision, and increment share one write transaction; concurrent increments do not overwrite one another. A committed count can remain if the client disconnects before receiving its response. Database waits are bounded. Public authentication, rate limits, destination reputation checks, and production availability guarantees are outside this local prototype.

## Run a generated service locally

The workflow starts from `skeleton/shortener/app.py`, implements the core service,
and adds only the requested supported capabilities. Completed source is retained
in the portable evidence bundle. The [completed example](../evidence/portable-bundle/candidates/892c93d5f5dea959a04e048edf463a157e937cccbf72a3dd78da9dddff186330/service/shortener/app.py)
contains core APIs, expiration and aliases. It is the accepted output of the
recovery scenario, built from the already accepted alias baseline.

From the repository root, after setup:

```sh
mkdir -p tmp
PYTHONPATH=evidence/portable-bundle/candidates/892c93d5f5dea959a04e048edf463a157e937cccbf72a3dd78da9dddff186330/service .venv/bin/python -B -c 'import uvicorn; from shortener.app import create_app; uvicorn.run(create_app("tmp/shortener-demo.sqlite"), host="127.0.0.1", port=8000)'
```

The service listens only on localhost. Open `http://localhost:8000/docs` for its
interactive API documentation. The database persists at `tmp/shortener-demo.sqlite`.
Example requests in another terminal:

```sh
curl -i -X POST http://localhost:8000/api/links -H 'Content-Type: application/json' -d '{"url":"https://example.com/synthetic","alias":"demo-link"}'
curl -i http://localhost:8000/r/demo-link
curl http://localhost:8000/api/links/demo-link/stats
```

These requests contact only the local service. The redirect response names the
destination; the service does not fetch it. Stop the development server with Ctrl-C.
