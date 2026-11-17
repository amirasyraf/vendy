# Compute Factory — POC Design

Status: draft for review · Date: 2026-11-15

## 1. Goal and scope

An Internal Developer Platform (IDP) where a user logs in, requests a VM, watches it get
built, and connects to it over SSH. Every VM is created the same way:

```
Bake (golden image)  ->  Provision (Terraform)  ->  Fry (Ansible stages)  ->  Ready (SSH via CA cert)
```

| Dimension | Long-term target | POC |
|---|---|---|
| OS types | Linux, Windows | Ubuntu Server only |
| Platforms | AWS, Azure, vSphere | AWS only |
| Fry stages | baseline, software, AD join, ... | baseline, software, AD join (mocked), access |

Guiding rules: **KISS**. Every OS- or platform-specific piece sits behind a directory or
variable boundary, so adding Windows/Azure/vSphere means **adding files, not refactoring**.

Out of scope for the POC: real AD, Vault, HA, production-grade security, cost controls,
image lifecycle cleanup, and a GHA pipeline to deploy the IDP.

## 2. Fixed decisions

| Item | Decision |
|---|---|
| AWS account | `amirasyraf` profile, `134584031874` (single account for everything) |
| Region | `ap-southeast-2` (Sydney) |
| GHA -> AWS | Existing OIDC role `arn:aws:iam::134584031874:role/github-aws-role` (trusts `repo:amirasyraf/*`). No static keys. |
| Repos (user `amirasyraf`; private except `compute-factory-platform`, public for free Actions minutes and checked for committed secrets first) | `compute-factory` (this doc, `./poc`, shared AWS infra), `compute-factory-idp`, `compute-factory-builder`, `compute-factory-platform` |
| Lifecycle | `./poc up` creates everything; `./poc down` removes everything, including user VMs (section 12) |
| Tagging | Every AWS resource, from Terraform, Packer or anywhere else, gets the tag `cf:poc=true`; `down` checks that none remain |
| IDP | Backstage (latest, v1.56.x at time of writing) on a single EC2, Docker Compose, Caddy TLS, `idp.amirasyraf.dev` |
| IDP deploy | Manual script from the laptop. No GHA. |
| Base image | Canonical's official Ubuntu AMI, looked up through the public SSM parameter `/aws/service/canonical/ubuntu/server/<ver>/stable/current/amd64/hvm/ebs-gp3/ami-id`. These are the same official images the Marketplace lists, without needing a Marketplace subscription. |
| Bake trigger | GHA in the builder repo, on push to `main` |
| Provision/Fry engine | GHA in the platform repo, triggered by Backstage |
| Software install | `winget` on Windows (future); `apt` on Ubuntu (POC). Same role interface. |
| VM access | SSH for every OS and platform, authenticated with SSH user certificates signed by a mock CA |
| Network | Default VPC in ap-southeast-2 (`vpc-23619045`, exists); VMs get public IPs; SG allows 22 from anywhere (POC) |

## 3. Architecture

```
 User (browser)                                 User (terminal)
     |  0. "Get SSH certificate" -> cert (instant)    |  ssh ubuntu@<ip>   (key + user cert)
     v                                                v
 idp.amirasyraf.dev  (EC2: Caddy -> Backstage -> Postgres)        VM (EC2, golden AMI)
     |  1. Software Template form -> mock ServiceNow REST (HTTP 201)  ^
     |  2. cf:vm:dispatch (quota) -> provision run in the `vms`      ^
     |     orchestrator, which dispatches one stage at a time ---+  |  terraform / ansible over SSH
     |     (retries, retry/skip, run state in its DB)            |  |  (pipeline uses its own short cert)
     |  5. reads catalog (vms/*/catalog-info.yaml)               |  |
     v                                                           v  |
 GitHub: compute-factory-platform                         GHA runner (OIDC -> AWS)
   - workflow: stage.yml (one stage per dispatch)            3. terraform apply (S3 state)
   - vms/<name>/  (inventory, written by stages)             4. fry stages (ansible), commit status
                                                                to vms/<name>/, ping Backstage refresh
 GitHub: compute-factory-builder
   - push to main -> packer + ansible -> golden AMI (tagged) ---------------^ (Terraform looks it up)
```

### Key design choice: Git is the VM inventory

Each VM has a folder `vms/<name>/` in `compute-factory-platform` that the stage workflow writes:

- `catalog-info.yaml`: the single desired-configuration source, a Backstage `Resource` entity with
  `spec.owner`, `spec.compute`, `spec.configuration`, optional `spec.billing` and `spec.cloudTags`.
  Annotations contain the coarse observed status, IP, SSH user, last run ID and request provenance.
  Per-stage status, attempts and retries live in the IDP orchestrator's database (see 6), not in Git.
  There is no separate `request.yaml` and no duplicate desired-input annotations.

That's all. No user keys or certs are stored per VM (see 7).

Example `catalog-info.yaml`:

```yaml
apiVersion: backstage.io/v1alpha1
kind: Resource
metadata:
  name: demo-vm-1
  annotations:
    cf/status: frying            # provisioning | frying | ready | failed | destroying
    cf/request-id: "<scaffolder task id>"   # VM incarnation, checked by every stage
    cf/public-ip: 3.25.x.x
    cf/ssh-user: ubuntu
    cf/requested-by: user:default/alice
    cf/run-id: "1234567890"
spec:
  type: vm
  owner: group:default/team-a    # the TEAM owns the VM
  lifecycle: poc
  compute: {platform: aws, os: ubuntu, size: S}
  configuration: {software: [git, nginx], adJoin: false}
  billing: {costCenter: CC-1234, projectCode: PROJ-5678}
  cloudTags: {environment: test}
```

The request template serializes desired spec as JSON, keeping requester identity and team claims
separate. The `register` stage validates/normalizes it and commits the entity before provisioning.
Terraform reads persisted spec into a temporary `.tfvars.json`; Ansible parameters and optional stage
decisions derive from that entity too. Refry persists software/AD configuration changes before
applying them. Observed-status writes never alter desired spec. Software refry remains additive,
not an uninstall/reconciliation engine. Editing Git alone does not trigger an apply.

The request form has one **Tags** table: `cost_center` and `project_code` (fixed keys, required
values, stored as `spec.billing`) plus optional extra tags (`spec.cloudTags`). They are self-reported
informational AWS tags, not trusted billing authorization. Custom tags cannot override
platform-controlled `Name`, `cost_center`, `project_code`, `cf:`, `aws:` or `billing:` keys. See
`CONTRACTS.md` §7 for types, validation and bounds. Future enterprise sections require explicit
validation and consumers; Backstage's permissive custom-spec storage is not itself validation.

Backstage registers one `Location` entity named `cf-vms` with a glob target,
`https://github.com/amirasyraf/compute-factory-platform/blob/main/vms/*/catalog-info.yaml`,
so new VMs show up automatically. When a VM folder is deleted, `catalog.orphanStrategy: delete`
removes its entity.

Why: it works the same way on every platform, it gives an audit trail for free, and Backstage
already knows how to read it. Only fast-changing orchestration state (stages, attempts, retries) is
kept out of Git, in the IDP orchestrator's database.
Trade-off: concurrent stage commits for different VMs can race, so the commit step uses a
`pull --rebase` retry loop. That's fine at POC volume.

## 4. Repositories

### 4.1 `compute-factory-builder` (Bake)

```
packer/
  ubuntu/                      # one dir per OS image
    ubuntu.pkr.hcl             # shared variables, build block, ansible provisioner
    source.aws.pkr.hcl         # amazon-ebs source (POC)
    # source.azure.pkr.hcl     # azure-arm  (future)
    # source.vsphere.pkr.hcl   # vsphere-iso/clone (future)
  # windows-2022/              # future
ansible/
  bake.yml                     # entry playbook, roles chosen by OS family
  roles/
    common/                    # e.g. updates, base packages (Linux tasks; Windows tasks later)
    ssh_ca/                    # installs CA pubkey + sshd TrustedUserCAKeys + AuthorizedPrincipalsFile
                               # CA PUBLIC key is NOT committed: bake reads it from SSM parameter
                               # /compute-factory/ssh-ca-pub (a fresh CA is created on every `poc up`)
images.yaml                    # build matrix: [{os: ubuntu, platform: aws, version: "26.04"}]
.github/workflows/bake.yml     # on push to main + workflow_dispatch (used by `poc up`):
                               # matrix from images.yaml -> packer build -only=<platform>
```

Bake does:
1. Starts from the latest official Ubuntu AMI (SSM parameter above).
2. Ansible `common`:
   - `apt` full upgrade, plus base packages `curl`, `jq`, `unzip` (nothing from the software catalog,
     so the `software` fry stage visibly does work);
   - timezone set to UTC;
   - writes `/etc/compute-factory/image-info.json` (image name, build date, git SHA);
   - cleans up at the end (`apt clean`, `cloud-init clean`, `machine-id` reset) so each VM boots fresh.
3. Ansible `ssh_ca`:
   - `/etc/ssh/trusted_user_ca_keys` is set to the CA pubkey.
   - An sshd drop-in sets `TrustedUserCAKeys`, `AuthorizedPrincipalsFile /etc/ssh/auth_principals/%u`
     and `PasswordAuthentication no`.
   - `/etc/ssh/auth_principals/ubuntu` contains `cf-automation` (the pipeline) and `team-platform-admins`.
   - The config is checked with `sshd -t` before reloading.
4. Produces an AMI named `cf-ubuntu-26.04-<timestamp>` with tags
   `cf:os=ubuntu`, `cf:os-family=linux`, `cf:os-version=26.04`, `cf:managed=true`, `cf:git-sha=<sha>`.

Platform agnosticism: Packer `-only=amazon-ebs.*` comes from the matrix. Adding Azure means adding a
source file and a matrix row. Ansible roles branch on `ansible_os_family` (Windows tasks go in
`tasks/windows.yml`).

### 4.2 `compute-factory-platform` (Provision + Fry)

```
terraform/
  platforms/
    aws/                       # root module for one VM on AWS (POC)
      main.tf variables.tf outputs.tf sizes.tf sizes.json
    # azure/  vsphere/         # future, same variable/output contract
ansible/
  fry/
    baseline.yml               # hostname, timezone, motd, banner
    software.yml               # installs the user's picks from software-catalog.yml (apt now, winget later)
    ad-join.yml                # MOCK: writes /etc/compute-factory/ad-join.json, logs "joined CORP.EXAMPLE"
    access.yml                 # writes the owning team's principal so team members' certs work
  software-catalog.yml         # the only software users may pick, with per-OS package IDs
  roles/ ...                   # per-stage roles with tasks/linux.yml (+ tasks/windows.yml later)
catalog/
  templates/
    vm-request/template.yaml   # Backstage Software Template: new VM
    vm-destroy/template.yaml   # Backstage Software Template: destroy VM
    vm-refry/template.yaml     # re-run one fry stage
    # ("Get SSH certificate" template lives in the idp repo; it doesn't touch this repo)
vms/                           # inventory (written by stage runs only)
.github/workflows/
  stage.yml                    # workflow_dispatch: run exactly one stage (dispatched by the IDP orchestrator)
```

**Fry stages (Ansible)**

| Stage | When | What it does |
|---|---|---|
| `baseline` | always | Hostname set to the VM name, `/etc/hosts`, login banner (VM, team, requester, platform), `/etc/compute-factory/vm-info.json` |
| `software` | if any software picked | Installs the picks using `software-catalog.yml` |
| `ad-join` | if picked (mock) | Writes `/etc/compute-factory/ad-join.json` (domain `corp.example.local`, OU, timestamp), adds a banner line, logs "joined CORP.EXAMPLE.LOCAL". No real AD. |
| `access` | always, last | Adds `team-<owning team>` to `/etc/ssh/auth_principals/ubuntu`, then `sshd -t` and reload |

**Software catalog** (`ansible/software-catalog.yml`). The POC set:

```yaml
git:    { linux: git,       windows: Git.Git }
htop:   { linux: htop,      windows: null }      # null = not offered on that OS
nginx:  { linux: nginx,     windows: null }
docker: { linux: docker.io, windows: null }
```

- On Linux the stage installs the `linux` names with `apt`. On Windows later it runs
  `winget install --id <windows id> --silent --accept-package-agreements --accept-source-agreements`.
- The request form's checkboxes list the same names, filtered by OS. They're maintained by hand next to this
  file, in the same repo.
- The `register` stage's validation rejects any name not in the catalog for the chosen OS, so the form
  can't be bypassed.
- To add software to an existing VM, use "Refry" → `software` with a new selection.

**Ansible conventions** (bake and fry):
- Every role's `tasks/main.yml` includes `tasks/linux.yml` or `tasks/windows.yml` based on `ansible_os_family`.
  Adding Windows means adding the `windows.yml` files, not changing the playbooks.
- Ansible connects over SSH on both OSes; Windows will use `ansible_shell_type: powershell`.
- The Ansible version is pinned and installed with `pip` on the runner, the same in both repos.

**Platform contract** (every `terraform/platforms/<p>/` root has the same interface):

| Inputs | Outputs |
|---|---|
| `name`, `os`, `size` (XS/S/M/L) or `instance_type`, `owner`, `tags` | `public_ip`, `ssh_user`, `instance_id`, `instance_type` |

- Each platform maps sizes in one data file. AWS: `sizes.json` (XS=`t3.micro`, S=`t3.small`,
  M=`t3.medium`, L=`t3.large`; all 2 vCPU, 1/2/4/8 GiB), read by `sizes.tf`. Regular users pick a size
  card showing the instance type and live AWS specs. A collapsed **Advanced** picker lists every x86_64
  instance type offered in the region, segmented by family type (general purpose, compute, memory,
  storage, accelerated, HPC) and grouped by family, with search, family/vCPU/memory filters, a
  current-generation toggle and sortable columns; the choice is stored as `compute.instanceType`.
  Demo mode greys out everything except `t3.micro`–`t3.large` and the IDP backend enforces it.
  VM pages show the launched type (`cf/instance-type`).
- Image lookup uses tags: `data "aws_ami"` with `cf:os=<os>`, `cf:managed=true`, `most_recent`.
- `ssh_user` comes from the OS: `ubuntu` for Ubuntu, `Administrator` for Windows later.
- State: S3 bucket `cf-tfstate-134584031874-apse2`, key `vms/<platform>/<name>/terraform.tfstate`, S3 native
  locking (`use_lockfile = true`). The workflow passes the key through `-backend-config`.

### 4.3 `compute-factory-idp` (Backstage)

```
backstage/                     # npx @backstage/create-app@latest
  app-config.yaml              # base
  app-config.production.yaml   # prod: domain, postgres, password auth, integration, catalog locations
  plugins/vm-status/           # custom frontend plugin: full VM pipeline/status card (see 5.3)
  plugins/cf-backend/          # backend: permission policy (5.1a), `cf:ssh:sign` (7), the `vms` plugin
                               # (VM orchestrator + demo mode, see 6) and the VM scaffolder actions
  catalog/org.yaml             # User + Group (team) entities = who may log in and their teams
  catalog/locations.yaml       # Location `cf-vms` (glob over the platform repo's vms/*/catalog-info.yaml)
  catalog/templates/ssh-cert/  # "Get SSH certificate" template
deploy/                        # (the IDP EC2 itself is created by the compute-factory repo's infra/, see 12)
  docker-compose.yml           # caddy, backstage, postgres
  Caddyfile
  deploy.sh                    # local: build image (linux/arm64) -> push changed layers to host-local registry via SSH tunnel -> compose up -d
  .env.example                 # IDP_USERS_B64 (password hashes), integration token, PG password,
                               # static token for the GHA -> catalog refresh call
                               # (no CA key here; it's read from Secrets Manager via the instance role)
```

- The image is built on the laptop (Apple Silicon, so arm64 matches t4g natively) and shipped over SSH.
  That avoids ECR and building on a small box.
- DNS: `idp.tf` in `~/projects/cloudflare/amirasyraf.dev` creates an A record to the EIP with `proxied = false`,
  so Caddy can get a Let's Encrypt cert. Leave it DNS-only, or switch to proxied after the cert is issued.

## 5. Backstage design

The `/catalog` list defaults to **Resource**. Normal users see only that kind; non-Resource list
filter links are normalized to Resource. Platform admins retain every kind in the picker and explicit
kind filter links. This is a frontend navigation restriction, not a new backend permission boundary;
User/Group data remains available for sign-in, ownership and template team pickers, and direct entity
pages retain existing authorization; team membership and admin access come from `catalog/org.yaml`.

### 5.1 Login, users and teams

- Username/password login (replaced GitHub OAuth). A small custom auth provider in the idp repo checks the
  password against scrypt hashes from env var `IDP_USERS_B64`, then signs in as the catalog `User` whose
  name equals the username. A signed HttpOnly cookie keeps the session for 12h.
- Passwords live only as hashes in `compute-factory/.poc-users` (gitignored, persists across cycles);
  `./poc set-password <user>` adds or changes one.
- `catalog/org.yaml` in the idp repo holds `User` and `Group` entities (teams, `spec.type: team`).
  Membership is `User.spec.memberOf: [team-a, team-b]`, and a user can belong to any number of teams.
  A user needs both an org.yaml entry and a password to log in. `catalog/org.yaml` is the
  source of truth for users, teams and memberships; do not duplicate its roster in documentation.
  Internal team IDs stay lowercase for owner refs and SSH principals.
- One extra group, `platform-admins`, can see and manage everything.
- On login, the token's ownership claims are the user plus their direct groups, e.g.
  `[user:default/alice, group:default/team-a, group:default/team-b]`. That's Backstage's default
  behaviour, so no custom resolver is needed.

### 5.1a Authorization model (team-scoped VMs)

Rule: **a VM is owned by exactly one team. A user can see and manage a VM only if they are a member of
its owning team** (or of `platform-admins`). VMs of other teams don't exist as far as that user is concerned.

Enforced in three places:

1. **Backstage permission framework** (`permission.enabled: true` plus one custom policy module of about 50 lines):
   - `catalog-entity` permissions on `Resource` entities of type `vm`: conditional on
     `isEntityOwner(claims)`. Every other entity (templates, users, groups, docs) is readable by all.
     Catalog lists, entity pages, the EntityPicker and catalog search therefore filter out other teams' VMs.
   - Unregistering or deleting catalog entities: `platform-admins` only, because VMs are removed through the destroy flow.
   - Scaffolder task read and cancel: `isTaskOwner`. Task logs contain inputs, so users only see their own runs.
2. **Templates**:
   - The team field only offers the user's own teams: the stock `MyGroupsPicker` field, which lists Groups
     with `relations.hasMember` = the current user.
   - Refry and destroy (`cf:vm:run`) read the VM entity with the initiating user's credentials, so a VM
     the user can't read fails before any run is created. Run pages, retry and skip are limited to the
     run's team and admins by the `vms` plugin.
3. **Stage guard** (defense in depth, because Backstage dispatches with a shared token):
   - The orchestrator passes `requester_teams`, derived server-side from the user's ownership claims, so
     the form can't override it.
   - `register` refuses unless the requested team is in `requester_teams`; every later stage refuses
     unless the group from persisted `spec.owner` is in `requester_teams` and the inventory's
     `cf/request-id` matches the run's VM incarnation.

Trust boundary for the POC: anyone with write access to the platform repo, or holding the integration
token, can bypass this. That's acceptable; they're platform admins.

### 5.2 Request flow (Software Template `vm-request`)

Form fields:

| Field | Values (POC) | Notes |
|---|---|---|
| Name | `^[a-z][a-z0-9-]{2,20}$` | Must be unique; the workflow validates |
| Team | one of the user's teams | Becomes `spec.owner`; preselected if the user is in exactly one team |
| Platform | `aws` (Azure/vSphere shown disabled or hidden) | Picks `terraform/platforms/<p>` |
| OS | `ubuntu` (Windows hidden) | Picks the image tag and fry task files |
| Size | S / M / L | Mapped per platform |
| Software | checkboxes: git, htop, nginx, docker (from the software catalog, filtered by OS) | None picked = `software` stage skipped |
| AD join | checkbox (mock) | baseline + access always run |

Steps:
1. `cf:servicenow:create` → a real HTTP POST to `https://idp.amirasyraf.dev/mock-rest/servicenow`.
   A small authenticated backend receiver returns HTTP 201 and a fake `RITM…` number, shown in task
   logs/output. This stateless mock demonstrates pre-GHA REST integration without ServiceNow, stored
   tickets or approvals. It sends VM name/team/compute and server-derived requester/task identity;
   failure prevents workflow dispatch. Caddy rewrites the public path to `/api/mock-rest/servicenow`;
   only Scaffolder service credentials are accepted. See `CONTRACTS.md` §8 for the exact interface.
2. `cf:vm:dispatch` → the authenticated `vms` backend (`POST /api/vms/requests`), which reserves a
   demo quota slot and creates a provision run in the orchestrator (6). `request_id` is the Scaffolder
   task ID, so a task retry or recovery returns the same run. Identity and teams are server-derived
   (see 5.1a). Demo controls and cleanup semantics are defined in `CONTRACTS.md` §8.1.
3. The task then finishes. Its output links to the run page (`/vms/runs/<id>`), the live view of the
   run: every stage with status, attempt, retry countdown, errors, the GitHub run and its steps, and
   Retry/Skip when a stage has failed for good. It does not depend on the VM's catalog entity, which
   appears a few seconds after `register` (see 5.3a). Scaffolder task recovery is on, so a backend
   restart mid-task resumes the task instead of failing it.
   The browser long-polls the task `/events` endpoint instead of SSE `/eventstream`, because
   TLS-inspecting corporate proxies (e.g. Netskope) buffer SSE and leave the page blank; see the IDP
   README for behavior and how to revert.
   Open tabs check for a newer deployed frontend and show a reload banner. Browser-side failures are
   reported to the backend log (`client-log` plugin), and Caddy keeps a JSON access log; see the IDP
   README "Logging and troubleshooting" and `./poc logs` / `./poc task`.
4. Output links: "Follow provisioning" (the run page) and "Open VM in catalog" (`resource:default/<name>`).

Backstage uses a **backend integration token** (a fine-grained PAT you create, scoped to
`compute-factory-platform`: Actions read/write, Contents read, **Commit statuses read**, which Backstage's
GitHub URL reader needs to resolve the commit SHA; without it, url Locations fail with 403). It
dispatches workflows and reads catalog files. Users therefore **don't need any access to the platform repo**.

### 5.3 Status and stages: run page and `vm-status` card

- The VM entity page shows the VM's latest orchestrator run (stages, attempts, errors, Retry/Skip),
  plus IP, SSH user, a ready-to-paste SSH command, desired configuration from structured spec, and
  links to certificate, refry and destroy. Home and the Resources list show each VM's latest run as a
  pipeline line from one `/api/vms/summary` call, falling back to `cf/status`.
- Run state comes from the `vms` plugin API, which applies the same team rule as the catalog (5.1a):
  members of the run's team and admins only. No GitHub token reaches the browser; the plugin polls
  GitHub server-side and stores job steps per attempt.
- Views poll about every 5 seconds while a run is running.

### 5.3a How status gets into Backstage quickly

1. Stages update the coarse `cf/status` in `vms/<name>/catalog-info.yaml` (commit with a `pull --rebase`
   retry loop). Fine-grained progress is in the orchestrator, which polls GitHub every 10 seconds.
2. Right after committing, the stage calls Backstage `POST /api/catalog/refresh`:
   - for the VM entity (`resource:default/<name>`), or for `location:default/cf-vms` on the first commit,
     so the new VM is discovered;
   - authenticated with a static external-access token restricted to catalog and the read-only
     demo cleanup check, stored as GHA secret `BACKSTAGE_TOKEN`.
3. Backstage re-reads the file. Expect a few seconds end to end. The call is best-effort: if it fails,
   the normal catalog refresh (a few minutes) catches up.

Why custom at all: the community GitHub Actions plugin lists every run in a repo, has no way to filter
to one VM's run, and would expose runs through a shared token regardless of team.

### 5.4 Manage / destroy

- The Create page lists only `vm-request` (`sub-page:scaffolder/templates` `templateFilter` in the IDP
  `app-config.yaml`). Destroy, refry and SSH certificate are reached from the VM page and Home; their
  direct template URLs still work, and the listing filter is not an authorization boundary.
- The VM entity page has links to the `vm-destroy` and `vm-refry` templates, prefilled with the
  entity (scaffolder `?formData=` query param). Both use an EntityPicker (kind Resource, type vm; it
  only lists the user's teams' VMs) and `cf:vm:run`, which creates a destroy or refry run.
- Team guard: see 5.1a.
- Day-2 actions in the POC:
  - Destroy (required). It supersedes any other active run of the VM.
  - Access: there's no per-VM action. Any member of the owning team connects with their personal SSH
    certificate (see 7).
  - **Refry**: pick a VM and a fry stage (software refry adds packages first). A new refry supersedes a
    failed refry; a running run or a failed provision must be retried, skipped or destroyed first.
  - **Retry / Skip** on a failed run, from the run page or the VM page (6.2).

## 6. Pipelines

Orchestration lives in the IDP; GitHub Actions executes. `CONTRACTS.md` §6 and §8.2 hold the exact
interfaces.

### 6.1 Runs and stages

The `vms` backend plugin (`compute-factory-idp/plugins/cf-backend/src/orchestrator.ts`) stores each
run and its stages in its database. Stage lists are code:

| Run | Stages (in order) |
|---|---|
| provision | `register` (validate + commit inventory) → `provision` (Terraform) → `baseline` → `software` (if picked) → `ad-join` (if picked, mock) → `access` → `finalize` (`cf/status: ready`) |
| refry | `configure` (persist the software/AD change; software and AD refry only) → the chosen fry stage → `finalize` |
| destroy | `destroy` (guards, `cf/status: destroying`, `terraform destroy`, delete state and inventory) → `finalize` (failure path only) |

A 10-second worker dispatches `stage.yml` for the next due stage, polls its GitHub run and job steps
(ETag requests), and advances the run. Each stage reads everything it needs from the committed
inventory (host, spec, provenance), so stages share no runner state and any one can run again alone.

### 6.2 Failure handling

- **Automatic retry**: up to 4 attempts per stage, waiting 30 s, 2 min and 8 min. An attempt still
  running 45 minutes after dispatch is cancelled and counts as failed. Failures in the stage's
  `Validate inputs` step, broken workflow files and rejected dispatches are permanent and are not
  retried.
- **Giving up**: the orchestrator runs `finalize` with `outcome: failed` (`cf/status: failed`,
  `cf/failed-at`) and marks the run failed. Retried transient failures never reach the inventory, so
  they don't start demo-mode failure timers. The VM is kept for debugging; destroy cleans up.
- **Manual**: on a failed run, team members and admins can **Retry** the failed stage (fresh attempt
  budget, then the run continues) or **Skip** it (`software` and `ad-join` only; the desired spec is
  unchanged). Everything else is retried or destroyed.
- **Transient IDP failures**: run state and due times are in the database, so a backend restart loses
  nothing. Every attempt is stored with its run key (`<run>.<stage>.<attempt>`, the end of the GitHub
  run title) before dispatch; a lost dispatch response is recovered by finding that title rather than
  dispatching again. Scaffolder task recovery resumes interrupted request tasks.

### 6.3 Idempotent stages

A stage can be dispatched again after any partial completion. `register` is a no-op for the same
request ID and refuses a different one. `provision` ignores AMI changes after creation, adopts an
instance tagged with the VM's `cf:request-id` that is missing from state instead of creating another,
and clears stale state locks. Fry stages are Ansible (`state: present`, templated files), so a re-run
converges; nothing is uninstalled. `destroy` and `finalize` succeed without changes when the inventory
is already gone. All stages are serialized per VM by the workflow concurrency group `vm-<name>`.

### 6.4 How a stage reaches the VM (fry stages)

1. Generate an ephemeral SSH keypair on the runner.
2. Fetch the CA private key from Secrets Manager (`compute-factory/ssh-ca`, via OIDC), sign the
   ephemeral key (principal `cf-automation`, validity 1h), then delete the CA key file from the runner.
3. Wait for SSH, then `ansible-playbook -i <ip>, -u <ssh_user> ansible/fry/<stage>.yml`.

No per-VM keys and no key pairs in Terraform. The pipeline is just another CA client, like the users.
For Windows later, Ansible connects over SSH as well (`ansible_shell_type: powershell`), because OpenSSH
Server is baked into the Windows image.

### 6.5 `bake.yml` (builder repo)

On push to `main` (paths `packer/**`, `ansible/**`, `images.yaml`): matrix over `images.yaml`, then OIDC,
then `packer init` and `packer build -only=amazon-ebs.ubuntu packer/ubuntu`. Packer's `ansible` provisioner
runs from the runner over SSH to the temporary build instance.

## 7. SSH CA (mock)

Model: **certs are per user, not per VM.** Every SSH client (users and the pipeline) holds its own keypair
and gets a short-lived cert from the CA. The cert's principals say which VMs it opens, and VMs never
store anyone's public key.

- The CA keypair is generated once locally with `ssh-keygen -t ed25519 -f cf_ssh_ca`.
- **Public key**: SSM parameter `/compute-factory/ssh-ca-pub`. Bake reads it and puts it into every image.
- **Private key**: stored in **one place**, the AWS Secrets Manager secret `compute-factory/ssh-ca`
  (ap-southeast-2). Two signers fetch it at signing time and keep it only in memory, never on disk:
  - the Backstage backend signs user certs. It reads the secret through the IDP EC2 instance role, which
    has `secretsmanager:GetSecretValue` on that one secret ARN only;
  - `stage.yml` (fry stages) signs the pipeline's throwaway certs. It reads the secret through the existing OIDC role.
- Rotation means updating the secret and re-baking with the new public key. Moving to Vault later only
  replaces the signing function in those two places.

| Who | Principals in cert | Validity | Issued by |
|---|---|---|---|
| Pipeline (fry) | `cf-automation` | 1h | `stage.yml`, ephemeral keypair per stage run |
| User | `team-<t>` for each team they're in, e.g. `team-a`, `team-b` | 12h | Backstage "Get SSH certificate" |
| Admin | adds `team-platform-admins` | 12h | same |

On each VM, `/etc/ssh/auth_principals/ubuntu` lists `cf-automation` and `team-platform-admins` (baked)
plus `team-<owning team>` (written by the `access` fry stage). A cert therefore opens exactly the VMs of
the user's teams, which is the same rule as visibility in the IDP (5.1a).

- Principals come from the user's catalog entity on the server side; the form can't choose them.
- The cert key ID (`-I`) is the Backstage username, so `/var/log/auth.log` on the VM shows who logged in,
  even though everyone uses the OS account `ubuntu`. Per-user OS accounts are not in the POC.
- Short validity is cheap because issuing is instant. It also means that removing someone from a team
  takes effect within 12h with no revocation list.

### 7.1 Why the user still provides a public key

An SSH certificate is the CA's signature over a public key. Only the holder of the matching private key
can use it. The pipeline does exactly the same thing: it generates a keypair and has the public key
signed. The difference is that the pipeline's key is throwaway.

- The user's public key is **only sent to the CA to be signed**. It is never installed on any VM, and the
  VM request form doesn't ask for it.
- The alternative, where the IDP generates a keypair and hands the user the private key, means private
  keys pass through the IDP. That's bad practice even for a POC, so I don't propose it.
- The browser remembers the last public key, so renewing is one click.

### 7.2 How a user connects

1. **Get a cert (once per day, or when it expires).** In the IDP, open **Get SSH certificate**, paste your
   public key (e.g. `~/.ssh/id_ed25519.pub`), and submit. A scaffolder action `cf:ssh:sign` in the
   Backstage backend signs it in about a second and shows the cert with a copy button. Nothing goes
   through GHA or Git.
2. **Save it as `~/.ssh/id_ed25519-cert.pub`**, next to the private key. OpenSSH picks it up automatically.
3. **Connect to any VM of your teams.** The VM page shows the command:
   ```
   ssh ubuntu@<public-ip>
   ```
4. **On the VM**, `sshd` checks three things:
   - the cert is signed by the baked CA (`TrustedUserCAKeys`);
   - one of its principals is listed in `/etc/ssh/auth_principals/ubuntu`;
   - it hasn't expired.

Check a cert with `ssh-keygen -L -f ~/.ssh/id_ed25519-cert.pub` (shows principals and expiry).

## 8. Adding Windows / Azure / vSphere later (no refactor)

| To add | Add |
|---|---|
| Windows OS | `packer/windows-2022/`, Windows task files in roles (OpenSSH Server + CA config, winget), OS row in `images.yaml`, `ssh_user` mapping, form option |
| Azure | `source.azure.pkr.hcl`, matrix row, `terraform/platforms/azure/` with the same contract, OIDC login step for Azure in `stage.yml` keyed on platform, form option |
| vSphere | Same pattern; needs a self-hosted GHA runner with network reach to vCenter |
| New fry stage | `ansible/fry/<stage>.yml` + role, one entry in the orchestrator's stage list and `stage.yml`'s stage enum, one form checkbox |

## 9. One-time manual steps (outside `./poc`)

1. **You**: create a fine-grained PAT for Backstage (GitHub doesn't allow creating it via the API) and put it
   in `compute-factory/.poc.env` (gitignored). It persists across up/down cycles.
   Set user passwords with `./poc set-password <user>`; they're kept in `compute-factory/.poc-users`.
2. Create the 4 private repos and push the code (done once, during the build).

Everything else is done by `./poc up` (section 12).

End-to-end test after `up`: get cert → request a VM → watch stages → SSH in → teammate SSH in → other team
can't see it or SSH in → destroy → `./poc down` → check that nothing is left.

## 10. Rough cost (running 24/7)

IDP t4g.medium about $30/mo plus EIP/EBS. Each test VM (t3.small) costs about $0.03/hr. AMI snapshots
cost cents. After `./poc down` the cost is $0: no AWS resources remain.

## 11. Open questions

Resolved:
- Ubuntu: latest LTS, 26.04.
- Day-2 actions: destroy, re-run fry stage; SSH access via personal certs.
- Team-owned VMs only; no personal VMs.
- VMs never expire; they're removed only through destroy.
- SSH: per-user, team-scoped certs from Backstage (instant, 12h). No per-VM access step.
- CA private key: single copy in AWS Secrets Manager, read by Backstage (instance role) and GHA (OIDC).
- Software: users pick from a fixed catalog in the request form (POC set: git, htop, nginx, docker).

- Lifecycle: `./poc up` / `./poc down` / `./poc status` (section 12).
- GitHub repos persist across cycles; `down` only clears their variables, secrets and `vms/`.
- `up`/`down` apply only the `idp` record in `~/projects/cloudflare/amirasyraf.dev` (`-target`).
- A 4th repo `compute-factory` holds `./poc`, `infra/` and this doc.

Open: none.

Verified during build:
- `catalog:fetch` runs with the initiating user's credentials (`ctx.getInitiatorCredentials()`, Backstage
  v1.56 source).
- Catalog search filters by the same catalog read permission (v1.56 source). A browser check is part of E2E.
- Scaffolder `?formData=` prefill is supported (v1.56 source). The VM card links use it.
- A Location `url` target with a glob works at runtime (the local smoke run loaded `cf-vms` and the platform templates).
- The team picker is the stock `MyGroupsPicker`; no custom field is needed.
- Terraform lock files must include `linux_amd64` hashes (`terraform providers lock -platform=linux_amd64
  -platform=darwin_arm64`). Otherwise `init` on the runner dirties the tree, and inventory commits need `--autostash`.
- The static catalog token is a service principal, so the ownership policy doesn't apply to it. That's by
   design: it permits catalog operations and the read-only demo cleanup check, not demo settings
   changes or VM admission.

## 12. Lifecycle: `./poc up` / `./poc down`

Goal: one command creates the whole POC from nothing, and one command removes all of it, including VMs
users launched. Every run assumes a clean slate. Existing resources and values are disposable, and
nothing is carried over between cycles except the code in Git, the two manual GitHub credentials (9), and
Caddy's TLS storage. `down` saves that to `.generated/caddy-data.tgz` on the laptop (gitignored) and `up`
restores it onto the new host, so a cycle doesn't request a new Let's Encrypt certificate (limit: 5 per week
for the same domain). Caddy renews it itself when it's within 30 days of expiry.

### 12.1 What lives where

```
compute-factory/                 # 4th repo: the "home" of the POC
  DESIGN.md
  poc                            # bash entrypoint: up | down | status
  .poc.env                       # gitignored: Backstage PAT (from 9)
  .poc-users                     # gitignored: username:scrypt-hash lines (./poc set-password)
  infra/                         # ONE Terraform root, LOCAL state (gitignored), applied only by ./poc
    main.tf                      #   default_tags { cf:poc = "true" } on the AWS provider
    state-bucket.tf              #   S3 bucket for VM Terraform state, force_destroy = true
    ssh-ca.tf                    #   tls_private_key (ed25519) -> Secrets Manager secret (recovery_window_in_days = 0)
                                 #   + SSM parameter /compute-factory/ssh-ca-pub
    idp.tf                       #   IDP EC2 (t4g.medium), EIP, SG, instance role (read the CA secret only)
    tokens.tf                    #   random_password: backend secret, PG password, BACKSTAGE_TOKEN
    outputs.tf
  ../compute-factory-idp, ../compute-factory-builder, ../compute-factory-platform   # sibling clones
```

- The CA key is created by Terraform, so it exists only in local state and Secrets Manager. A new CA is
  made on every `up`, and old certs stop working, which is the expected result for a clean slate.
- `recovery_window_in_days = 0` deletes the secret immediately, so the next `up` can reuse the same name.
- The infra state stays local because a remote state bucket would have to survive `down`. It's
  disposable like everything else.

### 12.2 `./poc up` (about 15–20 min; bake dominates)

1. **Preflight**:
   - check the tools (`terraform`, `gh`, `aws`, `docker`, `jq`), the `amirasyraf` AWS profile, `gh` auth
     and `.poc.env`;
   - fail fast if `infra` state already exists (the run assumes a clean slate; run `down` first).
2. **`terraform -chdir=infra apply`**: state bucket, CA secret + public key parameter, IDP EC2 + EIP, tokens.
3. **GitHub wiring** (`gh variable set` / `gh secret set` on the builder and platform repos): `AWS_ROLE_ARN`,
   `TF_STATE_BUCKET`, `BACKSTAGE_URL`, `BACKSTAGE_TOKEN`. Reset `vms/` in the platform repo to empty.
4. **Run in parallel**:
   - A. **Bake**: dispatch `bake.yml` once, retry run discovery without redispatching, then poll
     `gh run view --json status,conclusion` every 30 seconds. Status-read errors are retried (up to
     10 consecutive errors); monitoring has a one-hour limit. Only `completed` + `success` counts
     as success; other completed conclusions fail. Exhausted monitoring or unconfirmed dispatch/run
     discovery reports an unknown outcome, not a failed workflow. After confirmed success, query AWS
     for the latest managed Ubuntu AMI.
   - B. **IDP**:
     - DNS: targeted apply of the `idp` record in `~/projects/cloudflare/amirasyraf.dev`, with the EIP passed in;
     - write `.env` from Terraform outputs and `.poc.env`;
     - run `compute-factory-idp/deploy/deploy.sh`: build the image locally, ship it, `docker compose up -d`;
      - wait for `https://idp.amirasyraf.dev/.backstage/health/v1/readiness` to return 200, which proves
        the Backstage runtime and Caddy/TLS path are healthy.
5. **Done**: print the URL and a summary of what was created.

On failure or an unknown bake outcome, `up` leaves resources and workflows in place and still waits
for the parallel IDP deployment. Check `./poc status` and `.generated/logs/{bake,idp}.log`.
`./poc watch-bake <run-id>` resumes the same read-only bake monitor without dispatching a workflow,
changing GitHub wiring or resetting inventory. It exits 0 for success, 1 for a completed non-success
conclusion, and 2 for an unknown outcome. It does not check IDP health or AMI existence. If the run ID
was not discovered, find it in the builder's Actions page first. Do not rerun `up` to reconnect a
watcher; it is not a resume command. `down` is intentional teardown, not a monitoring recovery step.

### 12.3 `./poc down` (about 5–10 min)

Order matters: stop anything that could create resources, then destroy what Terraform knows about,
then sweep anything else, then verify.

1. **Freeze**:
   - cancel every queued or in-progress run in the platform and builder repos (`gh run list` / `gh run cancel`),
     so no VM or bake starts during teardown;
   - stop the Backstage containers, so no new requests come in.
2. **Destroy user VMs**:
   - for every `vms/*/*/terraform.tfstate` in the state bucket, run `terraform destroy` locally from the platform
     repo checkout with that state key;
   - run them in parallel (`xargs -P 5`), so time doesn't grow with the number of VMs;
   - this uses the same platform root module as provisioning, so it works for any platform added later.
3. **Sweep leftovers by tag** (catches failed provisions, interrupted bakes, missing state):
   - deregister AMIs tagged `cf:poc=true` and delete their snapshots;
   - terminate instances tagged `cf:poc=true`, including Packer build instances (Packer `run_tags`);
   - delete leftover `packer_*` key pairs and security groups.
4. **DNS**: targeted apply removing the `idp` record.
5. **`terraform -chdir=infra destroy`**: IDP EC2/EIP, CA secret, state bucket (force-destroy), tokens.
6. **GitHub cleanup**: delete the repo variables and secrets set in `up`, and commit an empty `vms/`.
7. **Verify**:
   - the Resource Groups Tagging API (`get-resources --tag-filters Key=cf:poc,Values=true`) returns nothing in
     ap-southeast-2;
   - no instances, AMIs or snapshots tagged `cf:poc` remain;
   - print CLEAN, or list what's left and exit non-zero.

`down` tolerates partial state: every step continues if its target is already gone, so it also cleans up
after a failed `up`. Running it twice is harmless.

### 12.4 `./poc status`

A read-only tag sweep plus `gh run list`. It shows what currently exists (IDP up?, AMIs, VMs, running
pipelines), so you can check before or after `up`/`down`.

### 12.5 Rules this puts on the rest of the design

- Every Terraform AWS provider (infra and `terraform/platforms/aws`) sets `default_tags { cf:poc = "true" }`.
  Packer sets `tags`, `run_tags`, `run_volume_tags` and `snapshot_tags` with `cf:poc = "true"`.
- No resource may be created outside Terraform or Packer, apart from Packer's temporary key pairs and
  security groups, which the sweep handles.
- Nothing outside AWS, the Cloudflare `idp` record and the GitHub variables, secrets and `vms/` is touched,
  so those are the only things `down` has to clean.
