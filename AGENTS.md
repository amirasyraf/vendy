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

## Troubleshooting lessons

- Start with `./poc logs [backstage|caddy|postgres|all] [since]` and `./poc task <task-id>` (read-only).
  Backstage logs browser-side failures as `Browser <kind>: ...` (`type: clientEvent`, with user, IP and
  bundle hash); Caddy's JSON access log has client IP, status and duration for every request.
- Ad-hoc host access: `ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
  ubuntu@$(terraform -chdir=infra output -raw idp_ip) bash -s <<'EOF' ... EOF` (the IP is reused across
  `./poc` cycles, so known_hosts entries go stale). Stack is `/opt/idp`. Add `</dev/null` to
  `docker compose exec -T` inside a here-doc, or it consumes the rest of the script.
- Scaffolder task state is in Postgres DB `backstage_plugin_scaffolder` (`tasks`, `task_events`). If the
  task page is blank but `task_events` has rows, the problem is delivery to the browser, not the task.
- Check who the client is early: `whois` the client IP. Corporate TLS-inspecting proxies (Netskope at
  `24.239.130.53` was observed) buffer long-lived responses. That is why the task page long-polls
  `/events` instead of SSE `/eventstream` (`compute-factory-idp/packages/app/src/taskEventPolling.ts`;
  revert steps are in that file and the IDP README). Prefer short, completing HTTP requests over
  SSE/WebSockets for user-facing live updates.
- After a frontend deploy, open tabs show a "new version" banner within a minute or on focus; a
  `Browser stale-bundle` log line means a user is still on the old bundle. Rule that out before
  concluding a deployed fix "didn't work".
- The static `BACKSTAGE_TOKEN` is restricted to `catalog` and `vms`; it cannot read scaffolder tasks or runs.
- VM orchestration state (runs, stages, attempts, GitHub run IDs) is in Postgres DB `backstage_plugin_vms`
  (`vm_runs`, `vm_run_stages`); read it with `./poc run <run-id>`. Backstage log lines `VM run <id> ...`
  trace dispatches, failed attempts, retries and outcomes; the stage's GitHub run title ends in `[<run>.<stage>.<attempt>]`.
