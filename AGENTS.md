# Vendy project instructions

## POC scope and verification

This is a POC: KISS. Do not add, restore, or recreate unit tests, unit-test suites, mocks or unit-test
scaffolding in any Vendy repository, including equivalent coverage under new filenames.
Previously removed unit tests must stay removed. Verify changes with appropriate static checks,
existing offline integration checks and focused browser/end-to-end smoke checks instead.
`./poc` is Python (`pocctl/`, stdlib + PyYAML): check it with `python3 -m py_compile poc pocctl/*.py`,
`uvx ruff check poc pocctl` (repo `ruff.toml`) and `uvx mypy --check-untyped-defs --ignore-missing-imports pocctl`, then exercise changed read-only
lookups against the live POC. Lifecycle mutations are only proven by a real `up`/`down` cycle. Do not
introduce speculative abstractions or test infrastructure without an explicit user request.

## Living documentation

`README.md` is for people: explain what the repo does, how to start, and where to look next.
`AGENTS.md` is for coding agents: give repo-specific constraints, boundaries, and verification rules.
Put details in their existing reference docs rather than duplicating them. Prune stale or generic
docs when their useful content has a better home.

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
tear down or recreate infrastructure just to deploy an application change. Docs-only work does not
need a deployment. Respect explicit instructions to leave the POC down.

## POC credentials

This is a POC: storing test credentials locally is expected; committing them is not. IDP login
passwords are in `.poc-passwords` (`username:password`, gitignored, mode 600; `./poc set-password`
maintains it alongside the hashes in `.poc-users`). Use them for signed-in live verification after every
IDP change instead of stopping at fixture checks or asking. When a new credential is created or shared,
store it there (or another gitignored file) immediately. Never commit or echo them into docs/logs.

## Pipeline time is the user's time

- Never use a GitHub Actions bake or stage run to discover bugs. Before dispatching, run every new or
  changed in-guest step (Ansible, Packer provisioner commands, sysprep, ec2launch) on a disposable
  scratch instance of the same OS/cloud, and run full `packer validate` (not `-syntax-only`).
- Every wait or poll loop (scripts, Packer inline PowerShell, workflows) must have a deadline and fail
  with the underlying error (e.g. sysprep's `setuperr.log`). Job timeouts are set from measured
  durations, not generous guesses; a hung step must fail in minutes, not at the job timeout.
- While a run is in progress, check its log for progress; a step repeating the same output means it
  is stuck. Cancel it and diagnose; do not wait for the timeout.
- Never destroy, stop or replace a user-visible VM (including test VMs the user may be using) without
  asking first.
- Tag authorized, disposable POC cloud resources with `cf:poc=true` wherever the resource supports
  tagging. Clean them up after verification; never use POC cleanup against unrelated resources.

## Troubleshooting boundaries

- Start with the read-only diagnostics in [OPERATIONS.md](OPERATIONS.md#diagnose-before-retrying).
  Do not treat `./poc status` output as proof that a destructive command is safe.
- Keep user-facing progress and sessions on short HTTP requests. The observed Netskope proxy at
  `24.239.130.53` buffered SSE; see `vendy-idp/packages/app/src/taskEventPolling.ts` and the IDP
  reference for the reason and revert steps.
- Leave `BACKSTAGE_TOKEN` restricted to catalog and the VM cleanup check. Do not use it to read tasks
  or runs. Preserve the `[<run>.<stage>.<attempt>]` suffix used for dispatch recovery.
