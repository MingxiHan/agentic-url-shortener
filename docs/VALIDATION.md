# Validation and limitations

The retained evidence belongs to the simplified offline implementation. Historical
online/fixed-answer modes, their exclusive tests and duplicate review records have
been removed. No API calls, package downloads or image pulls were used for this
validation. The installed Python environment and cached Docker image were reused.

## Recorded checks

| Check | Result | Evidence |
| --- | --- | --- |
| Harness/rule tests | 240 tests and 68 subtests passed; one upstream deprecation warning | [Test log](../evidence/validation/unit-tests.txt), [JUnit](../evidence/validation/unit-tests.xml) |
| Greenfield core | 11 tests passed | [Scenario summary](../evidence/scenario-summary.json) |
| Brownfield expiration | 15 tests passed | Same summary, with schema/API dependency plan and source changes |
| Rollback to core | 11 tests passed | Same summary and bundled rollback report |
| Alias clarification and revision | 14 tests passed for each of the two accepted-for-review candidates; stale approval rejected | Same summary, revisions 2 and 3 |
| Deliberate budget exhaustion and authorized replacement | Parent remains failed after two dispatches; replacement passes 16 tests and is accepted | Same summary and bundled handoff/decision lineage |
| Portable integrity check | 182 manifested files, five runs, four accepted releases verified | [Verification result](../evidence/validation/bundle-verification.json) |
| Independent execution from the retained bundle | Four accepted candidates passed: 11, 15, 14 and 16 tests | [Replay summary](../evidence/validation/replay/summary.json) |

[Checks and source hashes](../evidence/validation/checks.json) record commands and
source identity. The first scenario attempt used `/private/tmp`, which Colima did
not share: Docker rejected the bind mount before tests ran. The same source then
passed from the shared project directory. The [setup failure log](../evidence/validation/scenarios-setup-failure.txt) is retained.

The Docker image uses the same pinned package set as requirements.txt. The image
was not rebuilt during this offline validation. A redundant second requirements
file was removed; Dockerfile now copies requirements.txt directly. Fresh setup
still requires dependencies and image availability as explained in the README.

## What is actually tested?

`tests/` checks requirement parsing, source transformations, task dependency rules,
parallel execution, hash/path policies, stale approvals, persisted budgets,
terminal failure, explicit recovery, metrics and portable evidence integrity.
Some scheduler, retry, repair and Docker protocol tests use explicitly labeled
nonexecuting doubles. Those tests do not establish service correctness or real
automatic repair capability.

`acceptance/` and generated supplementary tests execute the actual candidate in
Docker during the scenario and replay checks. They cover HTTP APIs, persistent
state, concurrent count updates, invalid inputs, collision exhaustion, database
failures, expiration/migration and alias boundaries. Protected tests and generated
tests run separately, with mandatory IDs and real container exit checks.

The public evidence is a redacted derivative of the retained local records. The
original project directory is replaced with `/workspace/agentic-url-shortener`,
and the personal host name in the unit-test report is replaced with
`validation-host`. These are privacy placeholders, not the actual execution
location. Dependent file, artifact, context and manifest hashes and references
were recomputed for this derivative, including references in historical scripted
approval records. This does not represent new approvals or a new scenario run.
Runtime source, candidate content hashes, test outcomes, timings, run IDs and
decision actors are unchanged. The unredacted originals are retained privately.
Bundle verification checks the consistency of this public derivative; it does
not authenticate the original execution. The scenario summary's `portable_links`
provide direct relative links to the completed source and reports.
Only this current evidence set is needed; old run databases and duplicate exports
are not required to inspect or rerun the submitted candidates.

## Limits and trade-offs

- Local execution is a finite grammar and known-source transformation system. It
  cannot implement arbitrary requests or repair unknown defects. Agentic reasoning
  depth remains the central assessment risk.
- Scenario answers and approvals are scripted and labeled. Real human approvals
  use the same CLI gates. Operator names are not authenticated identities.
- Runtime review is structural rule checking, not an independent model review.
- These runs have no successful same-revision automatic repair sample. The MTTR
  proxy is N/A; approved replacement is recorded separately.
- Rollback selects code/evidence, not a deployed process or production database.
  The standalone rollback command does not perform the scenario's smoke test.
- Hash checks are unsigned integrity checks. The local operator and store are
  trusted; the prototype is not a hostile multi-tenant sandbox.
- SQLite and aggregate redirect counts keep the example small. There is no public
  deployment, production traffic/load guarantee, unique-visitor analytics or
  arbitrary repository support.

## External read-only review

An external read-only Claude review on 2026-10-20
examined the assignment text, current source/tests, generated services and recorded
results. It found no necessary capability lost during cleanup and no mandatory
fix blocking submission as a bounded offline prototype. Two evidence-file checks
were confirmed locally; the setup-failure log link was added above. Runtime code
and recorded execution evidence were unchanged at that review. The later public
export redacts machine identity as described above.

The review did not execute tests or containers. Its remaining concerns were
agentic reasoning depth, manual fallback semantics, the absent MTTR sample and
presentation quality; it does not guarantee assessment acceptance or a score.
