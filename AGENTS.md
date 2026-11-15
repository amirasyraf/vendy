# Compute Factory project instructions

## POC scope and verification

This is a POC: KISS. Do not add, restore, or recreate unit tests, unit-test suites, mocks or unit-test
scaffolding in any Compute Factory repository, including equivalent coverage under new filenames.
Previously removed unit tests must stay removed. Verify changes with appropriate static checks,
existing offline integration checks and focused browser/end-to-end smoke checks instead. Do not
introduce speculative abstractions or test infrastructure without an explicit user request.

## Living documentation

Documentation is living, breathing project material and must be kept current at all times.

- Treat stale or contradictory documentation as a defect, not as a follow-up task.
- Before editing code, inspect the relevant documentation in this repository and the affected
  sibling repository. Before finishing, update every README, design document, contract, example,
  workflow note, and operational procedure made inaccurate or incomplete by the change.
- Keep behavior, interfaces, configuration, security boundaries, lifecycle commands, UI behavior,
  and verification evidence synchronized with the implementation. Record important limitations and
  unknowns instead of implying that an unverified behavior works.
- Review documentation changes in the same verification pass as code changes. Do not land a code
  change with a known documentation mismatch.

The cross-repository contract is defined in `CONTRACTS.md`; update it when a shared annotation,
workflow input/output, environment variable, or file contract changes.

## Delivery

Background infrastructure work blocks only actions that consume its result. POC teardown,
restoration, deployment or builds do not block independent implementation, documentation or local
verification. If they need stable source, create separate worktrees and continue authorized work in
the same turn; do not wait for the live environment before editing code. Before yielding, identify
the exact pending output needed by every remaining action. Reading, planning and status updates
are not sufficient while safe implementation work remains. Live deployment/verification may depend
on the restored host; unrelated edits and offline checks do not. Never require a user reminder to
resume independent work.

Implementation is not complete when code is merely committed. After an implementation change is
verified and pushed, deploy the affected live environment and verify the deployed behavior before
reporting completion. For IDP changes, use `./poc deploy-idp` against the existing POC host; do not
tear down or recreate infrastructure just to deploy an application change.
