# Compute Factory — Build Contracts

This file pins every cross-repo interface. `DESIGN.md` explains *why*; this file fixes *exact names*.
If you need to change anything here, **stop and escalate to the orchestrator**. Don't invent a
variant, because other repos depend on these values.

## 0. Repos and local paths

| Repo (GitHub; private except `compute-factory-platform`, public for free Actions minutes) | Local path | Owner |
|---|---|---|
| `amirasyraf/compute-factory` | `~/projects/compute-factory` | orchestrator: `infra/`, `poc`, docs |
| `amirasyraf/compute-factory-builder` | `~/projects/compute-factory-builder` | Agent A |
| `amirasyraf/compute-factory-platform` | `~/projects/compute-factory-platform` | Agent A owns `ansible/` + `requirements-ansible.txt`; Agent B owns everything else |
| `amirasyraf/compute-factory-idp` | `~/projects/compute-factory-idp` | Agent C |

Default branch: `main`. Commit directly to `main`, with no PRs (POC). Use `git pull --rebase` before pushing.

## 1. AWS constants

| Name | Value |
|---|---|
| Account | `134584031874` |
| Region | `ap-southeast-2` |
| Local AWS profile | `amirasyraf` |
| GHA role (OIDC) | repo variable `AWS_ROLE_ARN` = `arn:aws:iam::134584031874:role/github-aws-role` (already set on builder + platform) |
| GHA region | repo variable `AWS_REGION` = `ap-southeast-2` (already set on builder + platform) |
| Mandatory tag | `cf:poc = "true"` on **every** AWS resource (Terraform `default_tags`; Packer `tags`, `run_tags`, `run_volume_tags`, `snapshot_tags`) |
| TF state bucket | `cf-tfstate-134584031874-apse2` (repo variable `TF_STATE_BUCKET` on platform) |
| VM state key | `vms/<platform>/<name>/terraform.tfstate`, e.g. `vms/aws/demo-1/terraform.tfstate` |
| CA private key | Secrets Manager secret id `compute-factory/ssh-ca`; `SecretString` = OpenSSH-format ed25519 private key |
| CA public key | SSM parameter `/compute-factory/ssh-ca-pub` (String; one OpenSSH public key line) |
| Network | default VPC; public IPs; SG ingress tcp/22 from `0.0.0.0/0` |

Tooling versions (pin these):
- Terraform `>= 1.10` (S3 `use_lockfile`), AWS provider `~> 6.0`.
- Packer latest 1.x with plugins `amazon` + `ansible`.
- `ansible-core`: one exact pinned version, chosen by Agent A, recorded in both repos' `requirements-ansible.txt`.
- Node 24 for Backstage.

## 2. Golden image (bake → provision)

- Base: official Canonical Ubuntu **26.04** amd64 (owner `099720109477` or SSM `/aws/service/canonical/ubuntu/server/26.04/stable/current/amd64/hvm/ebs-gp3/ami-id`).
- AMI name `cf-ubuntu-26.04-<YYYYMMDDhhmmss>`.
- AMI tags: `cf:os=ubuntu`, `cf:os-family=linux`, `cf:os-version=26.04`, `cf:managed=true`, `cf:poc=true`, `cf:git-sha=<sha>`.
  Snapshots get the same tags.
- Terraform selects the AMI with `most_recent = true`, `owners = ["self"]` and filters `tag:cf:os=<os>` and `tag:cf:managed=true`.
- Baked on the image:
  - `/etc/ssh/trusted_user_ca_keys` contains the CA pub key.
  - An sshd drop-in sets `TrustedUserCAKeys /etc/ssh/trusted_user_ca_keys`,
    `AuthorizedPrincipalsFile /etc/ssh/auth_principals/%u` and `PasswordAuthentication no`.
  - `/etc/ssh/auth_principals/ubuntu` contains the lines `cf-automation` and `team-platform-admins`.
  - `/etc/compute-factory/image-info.json` holds image name, build date and git SHA.
  - Base packages are `curl`, `jq`, `unzip` only. Do **not** bake anything from the software catalog.
- The bake workflow reads the CA pub key from SSM `/compute-factory/ssh-ca-pub` at build time. It is never committed.

## 3. SSH identity

| Principal | Meaning |
|---|---|
| `cf-automation` | pipeline (fry). Cert validity 1h, key ID `gha-<run_id>` |
| `team-<group>` | a member of Backstage group `<group>`, e.g. `team-alpha` |
| `team-platform-admins` | admins; baked into every VM |

- OS login user on Ubuntu: `ubuntu`.
- The fry stage `access` writes the **whole** `/etc/ssh/auth_principals/ubuntu` file with exactly three
  lines: `cf-automation`, `team-platform-admins` and `team-<owning team>`. Then it runs `sshd -t` and reloads.
- User certs (Backstage `cf:ssh:sign`):
  - validity 12h;
  - key ID = Backstage username;
  - principals = `team-<g>` for each group in the user's `spec.memberOf`, read server-side;
  - extensions: the defaults (`permit-pty` etc.).

## 4. Teams and users (Backstage `org.yaml`, idp repo)

- `compute-factory-idp/catalog/org.yaml` is the sole roster of users, teams and memberships.
- Teams are `Group` entities with `spec.type: team`; users declare membership in `spec.memberOf`.
- Usernames must match the password entries managed by `./poc set-password`.
- Group names are bare (`alpha`). The owner ref is `group:default/alpha`, and the principal is `team-alpha`.

## 5. Software catalog (platform repo `ansible/software-catalog.yml`)

```yaml
git:    { linux: git,       windows: Git.Git }
htop:   { linux: htop,      windows: null }
nginx:  { linux: nginx,     windows: null }
docker: { linux: docker.io, windows: null }
```

The `vm-request` template's software enum must be exactly `[git, htop, nginx, docker]`.

## 6. Platform repo: stage workflow

The IDP `vms` backend plugin is the orchestrator (§8.2); GitHub Actions only **executes one stage
per dispatch**. `provision.yml`, `fry.yml`, `destroy.yml` and `_fry-stage.yml` are removed. There is
no supported direct (non-IDP) VM lifecycle pipeline; a trusted manual `stage.yml` dispatch runs exactly
one stage and is not tracked by the orchestrator.

### 6.1 `stage.yml` (`workflow_dispatch`)

All inputs are strings and required unless noted.

| Input | Example | Notes |
|---|---|---|
| `stage` | `provision` | `register` \| `provision` \| `baseline` \| `software` \| `ad-join` \| `access` \| `configure` \| `destroy` \| `finalize` |
| `name` | `demo-1` | `^[a-z][a-z0-9-]{2,20}$` |
| `request_id` | Scaffolder task ID | VM incarnation, `^[a-zA-Z0-9_-]{1,100}$`; written once by `register`, checked by every other stage |
| `run_key` | `42.provision.2` | `<orchestrator run id>.<stage>.<attempt>`; unique per dispatch |
| `requested_by` | `amirasyraf` | Backstage user who created the orchestrator run (`demo-mode` for automatic cleanup) |
| `requester_teams` | `alpha,bravo` | comma-separated bare group names, server-derived by the IDP |
| `params_json` | `{}` | optional, default `{}`; stage-specific JSON object (below); max 40 KiB |

- `run-name: "stage <stage> <name> by <requested_by> [<run_key>]"`. The orchestrator recovers a lost
  dispatch response by this exact `[<run_key>]` suffix, so it must stay the end of the title.
- `concurrency: { group: vm-<name>, cancel-in-progress: false }`. The orchestrator dispatches at most one
  stage at a time per VM (destroy may be queued behind a still-running stage of a superseded run).
- `permissions: { contents: write, id-token: write }`. Executor code (scripts, Ansible, Terraform) comes
  from the dispatched commit (`github.sha`). Inventory is read from, and committed to, a separate
  checkout of current `main` taken when the job starts, because a stage queued behind another one must
  see that stage's inventory commits. Inventory commits keep the `pull --rebase` retry loop.
- `params_json` per stage:
  - `register`: `{"spec": <Resource spec, §7>}`;
  - `configure`: exactly one of `{"software": ["git", ...]}` (union into the recorded list) or `{"adJoin": true}`;
  - `destroy`: `{"demoGeneration": "<uuid>"}` for automatic demo cleanup, `{}` for a user destroy;
  - `finalize`: `{"outcome": "ready"}` or `{"outcome": "failed"}`;
  - every other stage: `{}`.
- **Failure classification.** All input, identity and authorization validation runs in a step named
  exactly `Validate inputs`, before any side effect. If that step fails, the orchestrator treats the
  failure as permanent and does not retry automatically. A `startup_failure` conclusion (broken
  workflow file) is also permanent. Every other failure, including timeouts, cancellation and runner
  loss, is treated as transient.

### 6.2 Stage semantics

Every stage is idempotent: re-dispatching it for the same `name` + `request_id` after any partial
completion converges and never duplicates resources, packages or inventory. Unless stated, a stage
first checks that `vms/<name>/catalog-info.yaml` exists with `cf/request-id == request_id`
(permanent failure otherwise) and applies the team guard to the persisted `spec.owner`.

| Stage | Behaviour |
|---|---|
| `register` | Validate `params_json.spec` with `scripts/spec.py`; team guard on the desired owner. Inventory with the same `cf/request-id` → success, no change. Inventory with a different request ID → permanent failure. Otherwise create the entity (§7) with `cf/status: provisioning`. |
| `provision` | `cf/status: provisioning`; `terraform apply`; record outputs; `cf/status: frying`. Never a second instance for the same incarnation: the AMI is ignored after creation, an untracked instance tagged `cf:vm=<name>` and `cf:request-id=<request_id>` is adopted into state, and a stale state lock cannot block retries indefinitely. |
| `baseline`, `software`, `ad-join`, `access` | `cf/status: frying`; read host and SSH user from inventory; run the stage playbook (§6.3) with variables derived from the persisted spec. |
| `configure` | Persist the desired configuration change from `params_json` (software union, or `adJoin: true`) through `spec.py`. No VM contact. |
| `destroy` | Inventory missing → success (already destroyed). With `demoGeneration`: the existing fail-closed cleanup check (`GET $BACKSTAGE_URL/api/vms/cleanup-check/<name>?generation=…&request_id=…`); ineligible → success without changes. Otherwise identity check, `cf/status: destroying`, `terraform destroy`, delete the state object, delete the inventory directory. A retry after partial completion converges. |
| `finalize` | Inventory missing or a different request ID → success without changes. `ready` → `cf/status: ready`, `cf/ready-at` (first time only). `failed` → `cf/status: failed`, `cf/failed-at` (first time only). |

Team guard: the persisted (or, for `register`, desired) `spec.owner` group must be in `requester_teams`;
`platform-admins` passes every guard. Authorization context is never part of the spec.
### 6.3 Ansible invocation contract (Agent B calls, Agent A implements)

```
ansible-playbook -i "<host>," -u <ssh_user> \
  --private-key <ephemeral_key> \
  --ssh-common-args "-o CertificateFile=<ephemeral_key>-cert.pub -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" \
  ansible/fry/<stage>.yml -e @<vars.json>
```

Run from the platform repo root. `vars.json` keys (all always present):

| Key | Type | Example |
|---|---|---|
| `cf_vm_name` | string | `demo-1` |
| `cf_team` | string | `alpha` |
| `cf_requested_by` | string | `amirasyraf` |
| `cf_platform` | string | `aws` |
| `cf_os` | string | `ubuntu` |
| `cf_software` | list | `["git","nginx"]` |
| `cf_ad_domain` | string | `corp.example.local` |

Stage playbooks: `ansible/fry/baseline.yml`, `software.yml`, `ad-join.yml`, `access.yml`.
Install with `pip install -r requirements-ansible.txt`.

## 7. VM inventory (platform repo `vms/<name>/`)

`catalog-info.yaml` is the only per-VM configuration file; `request.yaml` is removed:
```yaml
apiVersion: backstage.io/v1alpha1
kind: Resource
metadata:
  name: demo-1
  title: demo-1
  description: ubuntu VM on aws (S) for team alpha
  annotations:
    cf/status: provisioning        # provisioning | frying | ready | failed | destroying
    cf/public-ip: ""               # filled after provision
    cf/ssh-user: ubuntu
    cf/instance-id: ""
    cf/instance-type: ""   # launched EC2 type, set with cf/instance-id after apply
    cf/requested-by: amirasyraf
    cf/requested-at: "2026-11-15T12:00:00Z"
    cf/request-id: "scaffolder-task-id" # VM incarnation; equals the stage.yml request_id input
    cf/ready-at: "2026-11-15T12:03:00Z" # first readiness only; absent until ready
    # cf/failed-at is set on first recorded failure; neither timestamp is reset by refry
    cf/run-id: "1234567890"        # last stage.yml run that wrote this file
    cf/run-url: https://github.com/amirasyraf/compute-factory-platform/actions/runs/1234567890
spec:
  type: vm
  owner: group:default/alpha
  lifecycle: poc
  compute:
    platform: aws
    os: ubuntu
    size: S                # or, instead of size: instanceType: m7i.large (IDP Advanced picker)
  configuration:
    software: [git, nginx]
    adJoin: false
  billing:
    costCenter: CC-1234
    projectCode: PROJ-5678
  cloudTags:
    environment: test
```

- `spec` is desired state, annotations are observed results/provenance. No duplicated desired
  `cf/platform`, `cf/os`, `cf/size`, `cf/team`, `cf/software` annotations are written or consumed.
- `scripts/spec.py` is the workflow-boundary validator/normalizer. Required: `type: vm`, `owner`, and
  `compute.{platform,os}` plus exactly one of `compute.size` (a `sizes.json` key: XS/S/M/L) or
  `compute.instanceType` (EC2 type such as `m7i.large`; `mac*` rejected). Owner normalizes to
  `group:default/<team>`; other namespaces/kinds are rejected. Only AWS/Ubuntu are supported. Optional `lifecycle` defaults to `poc` (bounded
  nonempty string); `configuration` defaults to `{software: [], adJoin: false}`; billing/tags to `{}`.
  Software is a unique list of software-catalog keys; AD join is a boolean, not an encoded string.
  Unknown section fields, invalid types and control characters are rejected. Custom fields are not
  automatically schema-validated by the stock Backstage catalog.
- Billing supports only nonempty `costCenter` and `projectCode` strings up to 256 characters,
  projected as AWS `cost_center` and `project_code` tags (VMs launched before this change carry
  `billing:cost-center`/`billing:project-code`). The vm-request form (`tags` parameter, `ui:field:
  VmTagsEditor`: one table with fixed `cost_center`/`project_code` rows plus optional tags) and the IDP
  dispatch endpoint require both for new requests; `spec.py` keeps them optional so older specs still
  load. Values are self-reported, not trusted billing authorization. `cloudTags` is at most 40 string
  entries (keys 1–128 characters, values 0–256); `Name`, `cost_center`, `project_code` and prefixes
  `cf:`, `aws:`, `billing:` are reserved, case-insensitively.
  Mandatory platform tags cannot be overridden. Never store secrets in catalog data or tags.
- Terraform loads persisted spec and maps it to temporary `.tfvars.json` (`name`, `os`, `size`,
  `team`, original `requested_by`, `cloud_tags` including billing). It deletes that file on exit.
  Each stage derives Ansible variables and applicability from the committed entity.
- Refry persists desired configuration before applying: software refry adds the selected packages
  to the software list (union; an empty selection keeps it); AD refry enables `adJoin`. Other spec
  sections and original request provenance are preserved. Software installation remains additive:
  nothing is uninstalled, so the recorded list only grows.
- Status updates never rewrite spec. Direct Git edits do not trigger reconciliation or a Terraform
  update. New enterprise sections require explicit validation/consumer changes. Legacy inventory is
  not migrated; this change rolls out against an empty inventory after VM teardown.

- `cf/status` is the coarse, catalog-visible lifecycle written by stages (§6.2). Per-stage state,
  attempts, retries and errors live only in the IDP orchestrator database (§8.2); `cf/stages`,
  `cf/processes` and `cf/provision-run-id` are removed. `failed` is written only by `finalize` with
  `outcome: failed`, after the orchestrator has given up on a stage, so transient failures that are
  being retried never start demo-mode failure timers.
- `vms/.gitkeep` always exists.

## 8. Backstage ↔ platform integration

- `BACKSTAGE_URL` = `https://idp.amirasyraf.dev` (platform repo variable).
- `BACKSTAGE_TOKEN` (platform repo secret) is a Backstage static external-access token restricted to
  `catalog` and `vms`. Its subject is `compute-factory-workflows`. The `vms` plugin accepts it only on
  the read-only cleanup eligibility check; it cannot toggle settings, launch VMs or read run state.
- After each inventory commit, the workflow calls (best-effort, never fails the job):
  `POST $BACKSTAGE_URL/api/catalog/refresh` with header `Authorization: Bearer $BACKSTAGE_TOKEN` and JSON
  body `{"entityRef":"resource:default/<name>"}`. On the first commit it also refreshes
  `location:default/cf-vms`.
- Catalog locations (idp `app-config.production.yaml`, or `catalog/locations.yaml`):
  - Location entity **named `cf-vms`**, type `url`, `presence: optional`, target
    `https://github.com/amirasyraf/compute-factory-platform/blob/main/vms/*/catalog-info.yaml`
    (optional so an empty `vms/` succeeds and the last destroyed VM is deleted, not retained);
  - templates: `https://github.com/amirasyraf/compute-factory-platform/blob/main/catalog/templates/*/template.yaml`;
  - org and the ssh-cert template come from the idp repo itself.
- `catalog.orphanStrategy: delete`. Catalog processing interval should be short (POC).
- Template names (`metadata.name`): `vm-request`, `vm-destroy`, `vm-refry` (platform repo) and
  `ssh-cert` (idp repo). VM templates only **create an orchestrator run** and then finish. The whole
  pipeline, including non-VM steps such as the mock ServiceNow request, is an orchestrator run (§8.2):
  live progress, retry and skip are on the IDP run page (`/vms/runs/<id>`) and the VM entity page. When
  a VM task completes with a run, the IDP task page takes the browser to that run page.
  - `vm-request` has one step, `dispatch`, action `cf:vm:dispatch`, inputs `{name, specJson}` (unchanged
    spec JSON construction); outputs `{runId: number, runUrl: string}`. It calls `POST /api/vms/requests`
    with the initiating user's credentials; admission reserves demo quota and creates the provision run.
    `request_id` is the task ID, so a Scaffolder retry or recovery returns the same run.
    Template output links: `Follow provisioning` → `${{ steps.dispatch.output.runUrl }}`, and
    `Open VM in catalog` → `resource:default/<name>`.
  - `vm-refry` step `run`, action `cf:vm:run`, inputs `{operation: refry, vm: <entity ref>, stage,
    software}`; `vm-destroy` step `run`, action `cf:vm:run`, inputs `{operation: destroy, vm}`. Outputs
    `{runId, runUrl}`; link `Follow run` → `runUrl`. The action reads the VM entity with the initiating
    user's credentials (so the catalog permission policy authorizes it) and derives identity and teams
    server-side. The parameter forms are unchanged (`EntityPicker` with `catalogFilter: { kind: Resource,
    spec.type: vm }`; refry `stage` enum `[baseline, software, ad-join, access]`; `software` checkboxes).
  - The team picker is the stock `MyGroupsPicker` (Groups with `relations.hasMember` = the current user). It
    returns an entity ref, which the backend normalises to the bare name.
  - `github:actions:dispatch` and `cf:watch-workflow` are no longer used; the `CF_WORKFLOW_PROGRESS_V1`
    task-event format is removed.
- Custom scaffolder actions (idp repo): `cf:vm:dispatch`, `cf:vm:run`, and
  `cf:ssh:sign` (input `{ publicKey: string }`, output `{ certificate, principals, validUntil }`; reads the
  CA key from Secrets Manager `compute-factory/ssh-ca` (region from `AWS_REGION`) using default AWS
  credentials, which on EC2 come from the instance role through IMDS (hop limit 2 is set by infra)).
- Mock ServiceNow: the orchestrator's `servicenow` stage (§8.2) POSTs `{requestId, vmName, team,
  requestedBy, platform, os, size}` (`requestId` = the run's request ID, `requestedBy` = the requesting
  user's entity ref) to `computeFactory.mockServiceNowUrl` and requires a valid HTTP 201 response.
  Production URL: `https://idp.amirasyraf.dev/mock-rest/servicenow`; local default:
  `http://localhost:7007/api/mock-rest/servicenow`. The URL is backend-only config, same IDP origin,
  exact mock path, no redirects, ten-second timeout. The `mock-rest` plugin accepts only `plugin:vms`
  service credentials and returns `{number: "RITM" + ten digits, status: "created", mock: true,
  requestId}`. Numbers are deterministic per request ID; there is no persistent ticket store, real
  ServiceNow integration, approval or outcome update. The number appears in the stage `summary`, not in
  GHA inputs or VM inventory. Refry/destroy do not call it.

### 8.1 Demo mode control

- Part of the `vms` backend plugin (`/api/vms`): authenticated-user `GET /demo/state`, admin-only
  `POST /demo/settings` (`{enabled: boolean, generation: string}`), user `POST /requests`
  (`{name, specJson, requestId}` → `{runId, runUrl}`). `GET /cleanup-check/:name?generation=...&request_id=...`
  accepts only the workflow service subject and returns `{destroy: boolean}`. The `/demo/state` response
  shape is unchanged. Ordinary users cannot toggle mode or inspect others' control records.
- Defaults off. Settings and request/cleanup intent are persisted in the plugin database. One backend
  instance serializes admission, cleanup and orchestration. A 15-second worker reconciles immutable Git
  inventory, not the catalog projection. Concurrent request reservations count immediately;
  destroying/failed VMs retain their slots until inventory is removed. A reservation whose provision run
  ended without ever registering inventory (failed or cancelled) is released; such a run can no longer be
  retried, so the user submits a new request.
- Enabled: 3 VMs per original requesting account (including admins), reject a fourth portal request;
  when enabling, destroy oldest excess requests and retain newest 3. Ready VMs expire at
  `max(first_ready_at, enabled_at) + 5 min`; terminal failures use first failure time (`cf/failed-at`,
  written only after orchestration gives up) if never ready. Refry does not extend lifetime. Disable
  invalidates queued cleanup generations; re-enable starts fresh timers. Already-started destruction
  cannot be undone. Actions queues/Terraform add latency.
- Automatic cleanup creates an orchestrator `destroy` run (requested by `demo-mode`, `params_json`
  `{"demoGeneration": …}`, the VM's request ID). The stage checks the current generation and exact VM
  incarnation before destructive work; stale or missing VMs skip. Network/auth errors fail closed.
  Deadlines survive restarts; cleanup pauses while IDP/GitHub is unavailable. Orchestrator retries
  apply; a cleanup run that still fails is shown as "Cleanup stopped" for an operator (retry it from
  the run page), and a new generation creates a new run. A failed inventory scan blocks portal requests
  only while demo mode is enabled (quota fails closed).
- `computeFactory.demoAutomation: true` is production backend config. Default false makes local
  development/smoke safe: settings and read APIs can be exercised, but no inventory scan, launch,
  orchestration dispatch or cleanup runs. This is not a separate cloud scheduler or a hard
  termination-time/cost guarantee.

### 8.2 VM orchestrator (`vms` backend plugin)

The orchestrator owns stage order, retries, skips and run state in the plugin database. GitHub
Actions executes stages (§6). Stage lists are code, not configuration.

**Runs and stages.** One active run (`running` or `failed`) per VM name. Creating a `destroy` run
cancels any other active run for that VM; a new `refry` run cancels a failed refry run. Any other
creation while a run is active is rejected (409): a running run, or a failed (incomplete) provision,
must be retried, skipped or destroyed first.

| Run kind | Stages, in order |
|---|---|
| `provision` | `servicenow`, `register`, `provision`, `baseline`, `software`, `ad-join`, `access`, `finalize` |
| `refry` | `configure`, `<stage>`, `finalize` (`configure` applies only to software and ad-join) |
| `destroy` | `destroy`, `finalize` (`finalize` runs only on the failure path) |

- Stages run in one of two places. `servicenow` runs inside the IDP (an in-process handler); every
  other stage is a `stage.yml` dispatch (§6). In-process stages have no GitHub run (`runUrl: null`,
  empty `steps`); an HTTP 5xx, 429, timeout or network error is transient, any other unexpected
  response is permanent. An in-process attempt interrupted by a restart is simply run again (handlers
  are idempotent per request ID).
- Not-applicable stages (no software selected, no AD join, refry `configure` for baseline/access,
  destroy `finalize` on success) are `skipped` with `skippedBy: null`.
- Stage statuses: `pending | running | retrying | success | failed | skipped | cancelled`.
  Run statuses: `running | failed | succeeded | cancelled`.
- Automatic retry: at most 4 attempts per stage (1 + 3 retries), waiting 30 s, 2 min and 8 min before
  attempts 2–4 (`retrying` while waiting). An attempt that has not completed 45 min after dispatch is
  failed (and its GitHub run cancelled, best effort). Permanent failures (§6.1, and dispatch rejections
  other than 429/5xx) fail the stage immediately.
- When a stage fails for good, the orchestrator runs `finalize` with `outcome: failed` (same retry
  policy) and then sets the run to `failed`. If `finalize` itself fails, the run is `failed` without a
  further failure record. Success of every stage ends with `finalize` `outcome: ready` (provision,
  refry) and run `succeeded`.
- Manual actions, allowed only while the run is `failed`, for `platform-admins` and members of the run's
  team:
  - **retry** resets the failed stage to a fresh attempt budget (and `finalize` to pending), then
    continues the run from that stage;
  - **skip** marks the failed stage `skipped` (`skippedBy` = user) and continues with the next stage.
    Only `software` and `ad-join` are skippable. The desired spec is not changed by a skip.
- Dispatch recovery: the attempt is persisted (with its `run_key`) before dispatch. A lost dispatch
  response is resolved by finding the `stage.yml` run whose title ends in `[<run_key>]` among the newest
  100 runs; if none appears within 3 minutes, the attempt counts as a transient failure. Stages are
  idempotent, so a late duplicate is harmless.

**HTTP API** (`/api/vms`, user credentials; deliberate errors are `{ "error": string }`). Reads return
404 for runs the caller may not see: `platform-admins` see everything, others see runs whose team is
one of their groups.

| Route | Result |
|---|---|
| `GET /runs/:id` | `RunView` |
| `GET /runs?name=<vm>&limit=<1-20>` | `{ runs: RunView[] }`, newest first (default limit 10) |
| `GET /summary` | `{ vms: [{ name, latest: RunView }] }`: the newest run per VM name visible to the caller |
| `POST /runs/:id/retry` | `RunView`; 409 when not retryable, 403 when not allowed |
| `POST /runs/:id/skip` | `RunView`; 409 when the failed stage is not skippable or the run is not failed |

```ts
type RunView = {
  id: number;
  kind: 'provision' | 'refry' | 'destroy';
  name: string;
  team: string;                 // bare group name
  requestId: string;            // VM incarnation
  requestedBy: string;
  status: 'running' | 'failed' | 'succeeded' | 'cancelled';
  error: string | null;         // why the run failed or was cancelled
  createdAt: string;            // ISO 8601
  updatedAt: string;
  finishedAt: string | null;
  actions: { retry: boolean; skip: boolean };   // for this caller, on the failed stage
  stages: StageView[];
};
type StageView = {
  id: string;                   // e.g. 'provision', 'software', 'finalize'
  status: 'pending' | 'running' | 'retrying' | 'success' | 'failed' | 'skipped' | 'cancelled';
  attempt: number;              // 0 before the first dispatch
  maxAttempts: number;
  nextAttemptAt: string | null; // set while retrying
  startedAt: string | null;
  finishedAt: string | null;
  error: string | null;
  skippable: boolean;
  skippedBy: string | null;
  retriedBy: string | null;     // last user who manually retried this stage
  outcome: 'ready' | 'failed' | null;          // finalize only
  summary: string | null;       // one-line result, e.g. 'RITM0123456789 created (mock ServiceNow)'
  runUrl: string | null;        // GitHub run of the current or last attempt
  steps: { name: string; status: string }[];   // that run's job steps
  attempts: {                   // earlier attempts, oldest first
    attempt: number;
    runUrl: string | null;
    conclusion: string;         // success | failure | cancelled | timed_out | dispatch_failed | ...
    error: string | null;
    finishedAt: string;
  }[];
};
```

- `runUrl` from `cf:vm:dispatch`/`cf:vm:run` is `<app.baseUrl>/vms/runs/<id>`. The frontend polls
  `GET /runs/:id` (or `/summary`) about every 5 s while a run is `running`.
## 9. IDP runtime (orchestrator infra ↔ Agent C deploy)

- EC2: Ubuntu 26.04 **arm64** (t4g.medium). Cloud-init installs Docker Engine + compose plugin, puts
  `ubuntu` in the `docker` group, and creates `/opt/idp` owned by `ubuntu`. IMDSv2 has hop limit 2.
- SSH: `ssh -i ~/.ssh/id_ed25519 ubuntu@<ip>` (EC2 key pair from that key).
- `compute-factory-idp/deploy/deploy.sh <host> <env-file>`:
  - builds image `compute-factory-idp:latest` for `linux/arm64` locally;
  - copies `docker-compose.yml`, `Caddyfile` and `<env-file>` (as `.env`) to `/opt/idp`;
  - starts the host-local `registry` service (`127.0.0.1:15000` on the host only) and pushes the
    image to it through an SSH tunnel (only missing layers transfer);
  - runs `docker compose pull backstage` and `docker compose up -d`, reloads Caddy, then
    garbage-collects the registry and prunes unused host images.
  It must be idempotent and must not prompt.
- `.env` keys, produced by `./poc up`. Agent C must use exactly these:

| Key | Source |
|---|---|
| `IDP_DOMAIN` | `idp.amirasyraf.dev` |
| `IDP_USERS_B64` | base64 of `compute-factory/.poc-users` (`username:hash` lines; hash from `compute-factory-idp/deploy/hash-password.mjs`) |
| `GITHUB_TOKEN` | user's fine-grained PAT (integration: catalog reads + workflow dispatch) |
| `POSTGRES_PASSWORD` | random |
| `BACKSTAGE_TOKEN` | random (static external access token) |
| `AWS_REGION` | `ap-southeast-2` |
| `SSH_CA_SECRET_ID` | `compute-factory/ssh-ca` |

- Caddy terminates TLS for `IDP_DOMAIN` and proxies to the backstage container on port 7007.
  It rewrites only `/mock-rest/servicenow` to the authenticated `/api/mock-rest/servicenow` plugin route;
  other paths are unchanged. No new environment variable or external-access token is introduced.
- Health URL used by `./poc up`: `https://idp.amirasyraf.dev/.backstage/health/v1/readiness` must return 200.

## 10. Terraform platform contract (`terraform/platforms/aws`, Agent B)

- Backend: `backend "s3" {}` with partial config passed at init:
  `-backend-config=bucket=$TF_STATE_BUCKET -backend-config=key=vms/aws/<name>/terraform.tfstate -backend-config=region=ap-southeast-2 -backend-config=use_lockfile=true`.
- Variables: `name` (required), `os` (default `ubuntu`), `size` (default `S`), `instance_type` (default
  `""`; overrides `size`), `team` (default `""`),
  `requested_by` (default `""`), `request_id` (default `""`; VM incarnation, tagged as `cf:request-id`).
  **`terraform destroy -var name=<name>` must work with nothing else**,
  because `./poc down` uses that.
- Outputs: `public_ip`, `ssh_user`, `instance_id`, `instance_type`.
- Size map: `terraform/platforms/aws/sizes.json` in the platform repo, a JSON object of size
  (`XS`/`S`/`M`/`L` = `t3.micro`/`small`/`medium`/`large`) to EC2 instance type. `spec.py` and Terraform
  read it; the IDP `vm-sizes` backend reads the same file from `main` via the GitHub API for the request
  form's size cards (`ui:field: VmComputePicker` on `compute`) and adds live specs from
  `ec2:DescribeInstanceTypes`. Its Advanced picker lists every x86_64, EBS-backed type offered in the
  region (`ec2:DescribeInstanceTypeOfferings`; IDP host role, read-only). Terraform rejects types that
  cannot boot x86_64 and launches into a default subnet whose AZ offers the type.
- Demo mode: while on, the IDP accepts `compute.instanceType` only from `t3.micro`, `t3.small`,
  `t3.medium`, `t3.large` (`/api/vms/demo/state` returns them as `instanceTypes`); sizes are allowed.
- Resources are tagged `cf:poc=true`, `cf:vm=<name>`, `cf:team=<team>`, and `cf:request-id=<request_id>`
  when set. No key pair (the CA is used). The instance ignores AMI changes after creation, so a retry after
  a newer bake never replaces it.
- `./poc down` runs, for each key under `vms/aws/*/terraform.tfstate`:
  `terraform -chdir=~/projects/compute-factory-platform/terraform/platforms/aws init -reconfigure <backend-config…> && terraform … destroy -auto-approve -var name=<name>`.

## 11. Ground rules for all agents

- Only touch your own repo or paths. Never run `./poc down`, `terraform destroy` on infra you didn't create,
  or anything against other AWS resources in the account.
- Every AWS resource you create, including test ones, carries `cf:poc=true`. Clean up your own test resources.
- Long-running commands (builds, `yarn install`, waiting on GHA runs) use the shell tool's background mode,
  not `nohup`/`&`, and no sleep-polling. `gh run watch` in background mode is fine.
- No secrets in Git. `.env` files are gitignored.
