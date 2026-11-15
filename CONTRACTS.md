# Compute Factory — Build Contracts

This file pins every cross-repo interface. `DESIGN.md` explains *why*; this file fixes *exact names*.
If you need to change anything here, **stop and escalate to the orchestrator**. Don't invent a
variant, because other repos depend on these values.

## 0. Repos and local paths

| Repo (GitHub, private) | Local path | Owner |
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

## 6. Platform repo: workflows

All `workflow_dispatch` inputs are strings. Run names start with `<workflow> <name> by <requested_by>`;
provision appends `[<request_id>]`, destroy appends `[<demo_generation>:<expected_request_id>]` for
dispatch recovery. Optional manual-dispatch fields may be empty.

### 6.1 `provision.yml` inputs

| Input | Example | Notes |
|---|---|---|
| `name` | `demo-1` | `^[a-z][a-z0-9-]{2,20}$` |
| `spec_json` | serialized Resource spec | desired configuration as defined in §7; max 32 KiB |
| `requested_by` | `amirasyraf` | Backstage user name |
| `requester_teams` | `alpha,bravo` | comma-separated group names, server-side from Backstage |
| `request_id` | Scaffolder task ID | optional for trusted manual dispatch; stable VM incarnation |

### 6.2 Other workflow inputs

- `destroy.yml`: `name`, `requested_by`, `requester_teams`; optional `demo_generation` and
  `expected_request_id` for automatic cleanup. Both must match current policy/VM identity before deletion.
- `fry.yml`: `name`, `stage` (`baseline`|`software`|`ad-join`|`access`), `software` (JSON string array, default `[]`, used when stage=software), `requested_by`, `requester_teams`.
- `_fry-stage.yml` (`workflow_call`, internal): `name`, `stage`, `host`, `ssh_user`, `os`, `vars_json`.
  The caller grants `id-token: write` and `contents: write`.

Team guard: the group from the desired `spec.owner` must belong to `requester_teams`. On provision
the spec comes from the validated dispatch payload; destroy/refry read the persisted entity in
`vms/<name>/catalog-info.yaml`. A `platform-admins` member passes all guards. Authorization context
(`requested_by`, `requester_teams`) is not part of user-editable desired configuration.

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
    cf/stages: '{"provision":"pending","baseline":"pending","software":"pending","ad-join":"pending","access":"pending"}'
    cf/processes: '{"validate":"success","register":"success","provision":"running","fry-baseline":"pending","fry-software":"pending","fry-ad-join":"pending","fry-access":"pending","finalize":"pending","on-failure":"pending"}'
    cf/public-ip: ""               # filled after provision
    cf/ssh-user: ubuntu
    cf/instance-id: ""
    cf/requested-by: amirasyraf
    cf/requested-at: "2026-11-15T12:00:00Z"
    cf/request-id: "scaffolder-task-id" # manual:<provision-run-id> for manual dispatch
    cf/provision-run-id: "1234567890" # immutable; cf/run-id may later refer to refry/destroy
    cf/ready-at: "2026-11-15T12:03:00Z" # first readiness only; absent until ready
    # cf/failed-at is set on first recorded failure; neither timestamp is reset by refry
    cf/run-id: "1234567890"
    cf/run-url: https://github.com/amirasyraf/compute-factory-platform/actions/runs/1234567890
spec:
  type: vm
  owner: group:default/alpha
  lifecycle: poc
  compute:
    platform: aws
    os: ubuntu
    size: S
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
  `compute.{platform,os,size}`. Owner normalizes to `group:default/<team>`; other namespaces/kinds are
  rejected. Only AWS/Ubuntu and S/M/L are supported. Optional `lifecycle` defaults to `poc` (bounded
  nonempty string); `configuration` defaults to `{software: [], adJoin: false}`; billing/tags to `{}`.
  Software is a unique list of software-catalog keys; AD join is a boolean, not an encoded string.
  Unknown section fields, invalid types and control characters are rejected. Custom fields are not
  automatically schema-validated by the stock Backstage catalog.
- Optional billing supports only nonempty `costCenter` and `projectCode` strings up to 256 characters,
  projected as AWS `billing:cost-center` and `billing:project-code` tags. Values are self-reported,
  not trusted billing authorization. `cloudTags` is at most 40 string entries (keys 1–128 characters,
  values 0–256); `Name` and prefixes `cf:`, `aws:`, `billing:` are reserved, case-insensitively.
  Mandatory platform tags cannot be overridden. Never store secrets in catalog data or tags.
- Terraform loads persisted spec and maps it to temporary `.tfvars.json` (`name`, `os`, `size`,
  `team`, original `requested_by`, `cloud_tags` including billing). It deletes that file on exit.
  The provision job derives Ansible variables/optional-job decisions from the committed entity.
- Refry persists desired configuration before applying: software refry replaces the software list;
  AD refry enables `adJoin`. Other spec sections and original request provenance are preserved.
  Software installation remains additive: changing the desired list does not uninstall packages.
- Status updates never rewrite spec. Direct Git edits do not trigger reconciliation or a Terraform
  update. New enterprise sections require explicit validation/consumer changes. Legacy inventory is
  not migrated; this change rolls out against an empty inventory after VM teardown.

- Stage values: `pending | running | success | failed | skipped`.
- Stage keys are always these five, in this order: `provision`, `baseline`, `software`, `ad-join`, `access`.
- `cf/processes` is the complete workflow-job view, with the same status values and these keys in order:
  `validate`, `register`, `provision`, `fry-baseline`, `fry-software`, `fry-ad-join`, `fry-access`,
  `finalize`, `on-failure`. Optional software and AD jobs are `skipped`; `on-failure` is `skipped` on a
  successful run and `success` when it records a failed run.
- `vms/.gitkeep` always exists.

## 8. Backstage ↔ platform integration

- `BACKSTAGE_URL` = `https://idp.amirasyraf.dev` (platform repo variable).
- `BACKSTAGE_TOKEN` (platform repo secret) is a Backstage static external-access token restricted to
  `catalog` and `demo-mode`. Its subject is `compute-factory-workflows`. Demo mode accepts it only on
  the read-only cleanup eligibility check; it cannot toggle settings, launch VMs or read user state.
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
- Template names (`metadata.name`): `vm-request`, `vm-destroy`, `vm-refry` (platform repo, Agent B) and
  `ssh-cert` (idp repo, Agent C).
- `vm-request` dispatches with `cf:vm:dispatch`; refry/destroy retain `github:actions:dispatch`:
  - `vm-request` first runs step `servicenow`, action `cf:servicenow:create`, named
    `Create ServiceNow request (mock)`. Inputs: `{vmName, team, platform, os, size}` from the form;
    outputs: `{number: string, httpStatus: number}`. The action obtains requester identity from
    initiating credentials, POSTs those details plus `requestId` (task ID) and `requestedBy` (user entity
    ref) to `computeFactory.mockServiceNowUrl`, and requires a valid HTTP 201 response before dispatch.
    Production URL: `https://idp.amirasyraf.dev/mock-rest/servicenow`; local default:
    `http://localhost:7007/api/mock-rest/servicenow`. The URL is backend-only config, same IDP origin,
    exact mock path, no redirects, ten-second timeout and task-cancellation support.
    The `mock-rest` plugin accepts only `plugin:scaffolder` service credentials and returns
    `{number: "RITM" + ten digits, status: "created", mock: true, requestId}`. Numbers are deterministic
    per task ID; there is no persistent ticket store, real ServiceNow integration, approval or outcome
    update. Only the number/status appear in task logs/output, not GHA inputs or VM inventory.
    Template Editor dry-runs make the real stateless POST. Direct GHA dispatch and refry/destroy do not.
   - `cf:vm:dispatch` inputs `{name, specJson}`; outputs `{workflowRunId, workflowRunHtmlUrl}`. It uses
     initiating user credentials to call the demo-mode backend, which reserves quota and dispatches
     `provision.yml` against the fixed platform repo/main branch. Identity/team claims are server-derived;
     `request_id` is the task ID. No dry-run support; uncertain dispatches retain a visible reservation.
   - Refry/destroy use `repoUrl: github.com?owner=amirasyraf&repo=compute-factory-platform`, `branchOrTagName: main`;
  - pass `requested_by: ${{ user.entity.metadata.name }}`;
  - pass `requester_teams: ${{ user.entity.spec.memberOf | join(',') }}`. `memberOf` may contain refs like
    `group:default/alpha`, so the workflow normalises by stripping everything up to the last `/` or `:`.
  - VM pickers use `EntityPicker` with `catalogFilter: { kind: Resource, spec.type: vm }`.
  - The team picker is the stock `MyGroupsPicker` (Groups with `relations.hasMember` = the current user). It
    returns an entity ref, which the workflow normalises to the bare name.
  - Every GHA-backed template follows dispatch with `cf:watch-workflow`. That custom action polls the
     workflow's jobs and steps and streams the operation status into the Scaffolder task until it finishes.
     The watcher step ID remains `monitor`. Alongside human-readable logs it emits one-line messages
     prefixed `CF_WORKFLOW_PROGRESS_V1 ` followed by JSON: `{ version: 1, operation, status, conclusion,
     jobs: [{ name, status, startedAt, completedAt, steps: [{ name, status, startedAt, completedAt }] }] }`.
     Times are GitHub timestamps or null; completed statuses use the job/step conclusion. Missing jobs
     are `waiting` while active and `unknown` after completion, never assumed successful or skipped.
     The IDP task page uses these persisted, task-owner-authorized events for stage cards, timings and
     expandable steps. No credentials or new privileged GitHub endpoint enter the frontend contract.
   - The browser link to the run is the dispatch output `workflowRunHtmlUrl`. Stock GitHub dispatch also
     supplies `workflowRunUrl` (API URL); `cf:vm:dispatch` does not.
- Custom scaffolder action (idp repo, Agent C):
  - `cf:ssh:sign`, input `{ publicKey: string }`, output `{ certificate, principals, validUntil }`;
  - `cf:watch-workflow`, input `{ operation: provision|refry|destroy, workflowRunId: number }`, output
    `{ status: string }`; reads GitHub Actions job/step status with the server-side integration token and
    streams the complete operation until it finishes;
  - reads the CA key from Secrets Manager `compute-factory/ssh-ca` (region from `AWS_REGION`) using
    default AWS credentials, which on EC2 come from the instance role through IMDS (hop limit 2 is set by infra).

### 8.1 Demo mode control

- `demo-mode` backend: authenticated-user `GET /state`, admin-only `POST /settings`
  (`{enabled: boolean, generation: string}`), user `POST /requests` (`{name, specJson, requestId}`).
  `GET /cleanup-check/:name?generation=...&request_id=...` accepts only the workflow service subject
  and returns `{destroy: boolean}`. Ordinary users cannot toggle mode or inspect others' control records.
- Defaults off. Settings and request/cleanup intent are persisted in the plugin database. One backend
  instance serializes admission and cleanup. A 15-second worker reconciles immutable Git inventory,
  not the catalog projection. Concurrent request reservations count immediately; destroying/failed VMs
  retain their slots until inventory is removed. Repository writers remain trusted and can bypass
  portal admission through direct workflow dispatch; reconciliation still applies policy to their VMs.
- Enabled: 3 VMs per original requesting account (including admins), reject a fourth portal request;
  when enabling, destroy oldest excess requests and retain newest 3. Ready VMs expire at
  `max(first_ready_at, enabled_at) + 5 min`; terminal failures use first failure time if never ready.
  Refry does not extend lifetime. Disable invalidates queued cleanup generations; re-enable starts
  fresh timers. Already-started destruction cannot be undone. Actions queues/Terraform add latency.
- Automatic destroy checks the current generation and exact VM incarnation before guard validation
  and again immediately before destructive work; stale or missing VMs skip. Network/auth errors fail
  closed. Deadlines survive restarts; cleanup pauses while IDP/GitHub is unavailable. Ambiguous
  dispatches are recovered from the newest 100 matching workflow-dispatch runs; unresolved cases
  remain visible/reserved rather than being blindly redispatched. Operator intervention may be needed.
- `computeFactory.demoAutomation: true` is production backend config. Default false makes local
  development/smoke safe: settings can be exercised, but no inventory scan, launch or cleanup runs.
  This is not a separate cloud scheduler or a hard termination-time/cost guarantee.

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
- Variables: `name` (required), `os` (default `ubuntu`), `size` (default `S`), `team` (default `""`),
  `requested_by` (default `""`). **`terraform destroy -var name=<name>` must work with nothing else**,
  because `./poc down` uses that.
- Outputs: `public_ip`, `ssh_user`, `instance_id`.
- Resources are tagged `cf:poc=true`, `cf:vm=<name>`, `cf:team=<team>`. No key pair (the CA is used).
- `./poc down` runs, for each key under `vms/aws/*/terraform.tfstate`:
  `terraform -chdir=~/projects/compute-factory-platform/terraform/platforms/aws init -reconfigure <backend-config…> && terraform … destroy -auto-approve -var name=<name>`.

## 11. Ground rules for all agents

- Only touch your own repo or paths. Never run `./poc down`, `terraform destroy` on infra you didn't create,
  or anything against other AWS resources in the account.
- Every AWS resource you create, including test ones, carries `cf:poc=true`. Clean up your own test resources.
- Long-running commands (builds, `yarn install`, waiting on GHA runs) use the shell tool's background mode,
  not `nohup`/`&`, and no sleep-polling. `gh run watch` in background mode is fine.
- No secrets in Git. `.env` files are gitignored.
