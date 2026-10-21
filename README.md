# Agentic URL Shortener

A local engineering harness that takes a supported requirement through planning,
source changes, tests, review and human release acceptance. The example product is
a FastAPI/SQLite URL shortener. The harness uses finite English rules and authored
code fragments, not a runtime language model. It needs no API key and makes no
network calls during planning or validation.

## What is in the project?

| Location | Purpose |
| --- | --- |
| `orchestrator/` | CLI, dependency graph, bounded execution, state, approvals, source transformations and evidence |
| `skeleton/` | Deliberately unimplemented starting contract for the greenfield scenario |
| `orchestrator/templates/` and `orchestrator/domain.py` | Core implementation and supported feature transformations |
| `runner/` | Isolated Docker validation of generated candidates |
| `acceptance/` | Protected acceptance tests for the URL shortener |
| `tests/` | Tests of the harness, rules, state and safety controls |
| `tools/run_local_scenarios.py` | Reproduce the three scenarios and bounded recovery demonstration |
| `evidence/` | One retained validation set, including actual generated URL-shortener source |
| `docs/` | Architecture, API and validation details |

The skeleton is intentionally not a working service. The workflow implements it
and retains each candidate under `evidence/portable-bundle/candidates/`. See
[API.md](docs/API.md) for a direct link to a completed service and how to run it.

## Setup

Use Python 3.11+ on macOS or Linux (Windows requires WSL), and a running Docker
daemon. Validation used Python 3.13.5. A new machine needs the pinned packages and
Docker image below; obtaining them normally requires internet access. Once those
prerequisites are installed, workflow execution and validation are offline. No
first-install air-gapped bundle is supplied.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p tmp
docker build --rm=false -f runner/Dockerfile -t agentic-shortener-runner:local .
.venv/bin/python -B -m orchestrator doctor
```

For an offline run, reuse installed dependencies and a cached runner image.
`AGENT_RUNNER_IMAGE` can select another already-built image. No `.env` file is
required.

## Reproduce and verify

Run the complete workflow in a new directory:

```sh
.venv/bin/python -B -m tools.run_local_scenarios --runs-dir tmp/demo-NEW
```

| Scenario | Observable work |
| --- | --- |
| Greenfield | Implement creation, redirects, aggregate counts and health from the skeleton |
| Brownfield | Inspect accepted core code, order schema before API changes, add expiration, preserve links/counts, then roll back and smoke-test core |
| Ambiguous | Clarify easier sharing as aliases; implement eight-character aliases; revise to 4–32, invalidate the old approval and re-plan |

The script also deliberately exhausts a two-dispatch budget, records a terminal
handoff and explicitly authorizes one replacement run from the accepted baseline.
All scripted answers/approvals are labeled `scripted-local-demo-operator`; they
exercise the gates and do not represent independent human review.

The command retains a state database, source snapshots, validation reports,
`scenario-summary.json` and a portable bundle. Use a fresh `NEW` path on each run.
The [retained summary](evidence/scenario-summary.json) links requirements, plans,
source changes, validation and metrics from the submitted execution.

Verify retained evidence without executing candidate code:

```sh
.venv/bin/python -B -m orchestrator verify-bundle evidence/portable-bundle
```

Independently rerun the accepted candidates in Docker:

```sh
.venv/bin/python -B -m orchestrator verify-bundle evidence/portable-bundle --rerun --output tmp/replay-NEW
```

Run the harness tests:

```sh
.venv/bin/python -B -m pytest tests -q --basetemp=tmp/tests-NEW
```

See [validation](docs/VALIDATION.md) for recorded results and the public evidence
redaction notice. Hash verification checks
consistency, not independent authenticity of historical execution. Candidate replay
retests outputs; the scenario script also repeats planning and code changes.

## Human-operated workflow

```sh
.venv/bin/python -B -m orchestrator start greenfield
.venv/bin/python -B -m orchestrator inspect RUN_ID
.venv/bin/python -B -m orchestrator events RUN_ID
.venv/bin/python -B -m orchestrator approve RUN_ID --action accept --hash CURRENT_HASH --actor reviewer
.venv/bin/python -B -m orchestrator start brownfield --baseline ACCEPTED_RUN_ID
```

Replace placeholders with actual output. Inspect the plan, candidate and evidence
before deciding. Existing-baseline plans, including alias requirement revisions, require
`approve ... --action plan`, followed by `resume RUN_ID`. Final acceptance selects
the validated local code/evidence snapshot; it does not deploy a service.

For the ambiguous scenario, start it against the accepted core baseline, then:

```sh
.venv/bin/python -B -m orchestrator answer RUN_ID --text 'Use optional custom aliases, exactly eight characters; return 409 on collisions.'
.venv/bin/python -B -m orchestrator resume RUN_ID
```

Use `approve ... --reject` to reject a gate or `stop RUN_ID` to stop work. Actor
names are local attribution labels, not authenticated identities. The run store
defaults to `runs/`; put `--runs-dir PATH` before each subcommand to choose another.

A recoverable terminal handoff may permit one human-authorized replacement:

```sh
.venv/bin/python -B -m orchestrator recover FAILED_RUN_ID --hash HANDOFF_HASH --actor reviewer --instructions 'Use the standard bounded budget; preserve the accepted contract.'
.venv/bin/python -B -m orchestrator resume NEW_RUN_ID
```

The parent remains terminal with spent budgets intact. The replacement has its own
visible budget and normal gates; it cannot create another recovery run. Policy or
integrity failures require manual review. `rollback TARGET_RUN_ID
--expected-current CURRENT_RELEASE_HASH --actor reviewer` selects a previously
accepted snapshot. It does not change a running service/database or automatically
smoke-test; the scenario script performs that test explicitly.

## Supported input and limitations

Examples of complete supported statements:

- `Build a URL shortener with creation, redirects, aggregate counts and health.`
- `Create a core URL shortener with redirects and counts.`
- `Add optional link expiration while preserving existing links and counts.`
- `Make links easier to share.`
- `Use optional custom aliases, exactly eight characters; return 409 on collisions.`
- `Change custom aliases to 4-32 characters; duplicate aliases return 409.`

Alias bounds satisfy `4 <= minimum <= maximum <= 32`; collision behavior is 409.
Unsupported clauses, conflicting requirements and unknown source structures pause
or stop. The implementation cannot interpret arbitrary prose, add arbitrary
features or repair unknown bugs. Runtime review is a structural rule check.

The PDF does not explicitly mandate an API or runtime model. However, finite rules
may fall short of the evaluator's expectations for agentic requirement understanding
and codebase reasoning. This is a bounded prototype, not general autonomous
software engineering. [Design](docs/DESIGN.md) maps the mechanisms and their limits
to the assignment.
