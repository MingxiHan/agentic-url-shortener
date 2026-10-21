# Docker validation

`DockerRunner` validates candidates produced by the offline local workflow.
Prepare its image explicitly during setup:

```sh
docker build -f runner/Dockerfile -t agentic-shortener-runner:local .
```

Preflight requires an available Docker daemon and an existing image. The runner uses
that image's immutable ID and never builds, pulls, or switches to host execution
automatically. Each validation creates a separate labeled container with no network,
non-root execution, a read-only root filesystem and mounts, dropped capabilities,
CPU/memory/process limits, and a 128 MiB writable temporary filesystem. No host
writable directory, home directory, credentials, or Docker socket is mounted.

The trusted supervisor runs protected and generated tests in separate pytest
processes. Plugin autoload, candidate configuration, conftest discovery, bytecode
writes, and pytest's cache provider are disabled. The host requires the exact
protected test IDs in `acceptance/manifest.json`, complete successful test phases,
no skipped/xfail/xpass outcomes, consistent JSON/JUnit evidence, and the actual
container exit status. Successful runs also export real OpenAPI and SQLite schema
artifacts. The supervisor streams bounded evidence to the host; the host writes a
new output directory and preserves the report, logs, artifacts, and their hashes.

Timeout, stop, and excessive output terminate only the container carrying the
runner's ownership label. Containers, candidates, reports, and temporary evidence
are retained. There is no automatic file or container removal; cleanup requires
the user's explicit permission. Inspect a retained container using the name in its
report, for example `docker inspect <container-name>`.

Actual Docker runs cover the greenfield, brownfield, and ambiguous scenarios,
plus rollback and bounded replacement recovery. See
[`docs/VALIDATION.md`](../docs/VALIDATION.md) for their retained results and
reproduction commands. Runner unit tests also exercise isolation and failure
boundaries with controlled subprocess fakes.

The protected Python tests share a process with the service under test;
the system does not claim resistance to deliberately hostile Python manipulating
its own test process. Read-only tests, separate test sessions, policy review, and
human acceptance reduce risk within this local prototype's scope.
