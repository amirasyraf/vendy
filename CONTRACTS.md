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
| Mandatory tag | `cf:poc = "true"` on **every** AWS and Azure resource (AWS Terraform `default_tags`; Packer `tags`, `run_tags`, `run_volume_tags`, `snapshot_tags`, `azure_tags`; Azure Terraform per resource) |
| TF state bucket | `cf-tfstate-134584031874-apse2` (repo variable `TF_STATE_BUCKET` on platform) |
| VM state key | `vms/<platform>/<name>/terraform.tfstate`, e.g. `vms/aws/demo-1/terraform.tfstate` |
| CA private key | Secrets Manager secret id `compute-factory/ssh-ca`; `SecretString` = OpenSSH-format ed25519 private key |
| CA public key | SSM parameter `/compute-factory/ssh-ca-pub` (String; one OpenSSH public key line) |
| Network | default VPC; public IPs; SG ingress tcp/22 from `0.0.0.0/0` |

### 1a. Azure constants

AWS remains the shared base (SSH CA, Terraform state bucket, IDP host); Azure holds only VMs and images.

| Name | Value |
|---|---|
| Tenant / subscription | `c26545da-80fa-4c31-9a8f-a4ae475bd17e` / `74b39a79-f79d-4adb-8ae0-fe9118fc564a` |
| Location | `australiaeast` |
| GHA identity (OIDC) | Entra app `cf-github-actions` (`2e6cce7d-…`), Contributor on the subscription; federated subjects `repo:amirasyraf@15522007/compute-factory-{builder@1458285691,platform@1458285776}:ref:refs/heads/main` |
| GHA repo variables (builder + platform) | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_LOCATION` |
| Image gallery | resource group `cf-images`, gallery `cfimages`, one definition per OS release (`ubuntu`: Ubuntu 26.04, Linux, Gen2, x64, NVMe + SCSI); the current definition is created by `infra/azure.tf` |
| Per-VM resources | resource group `cf-vm-<name>`; VNet, subnet, NSG (tcp/22 from anywhere), static Standard public IP, NIC and VM all named `cf-<name>` |
| IDP reader | service principal `cf-idp-azure-reader` (Reader, certificate); Secrets Manager `compute-factory/azure-reader`, `SecretString` JSON `{tenantId, clientId, subscriptionId, location, certificatePem}` |
| Laptop admin | service principal `cf-admin-local` (Owner, certificate; `ARM_*` in `~/.zshrc`) for `infra/` and `./poc` |

Tooling versions (pin these):
- Terraform `>= 1.10` (S3 `use_lockfile`), AWS provider `~> 6.0`.
- Packer latest 1.x with plugins `amazon`, `azure` (`>= 2.6, < 3`) + `ansible`. azurerm provider `~> 4.0`.
- `ansible-core`: one exact pinned version, chosen by Agent A, recorded in both repos' `requirements-ansible.txt`.
- Node 24 for Backstage.

## 2. Golden image (bake → provision)

- Base: official Canonical Ubuntu **26.04** amd64 (owner `099720109477` or SSM `/aws/service/canonical/ubuntu/server/26.04/stable/current/amd64/hvm/ebs-gp3/ami-id`).
- AMI name `cf-ubuntu-26.04-<YYYYMMDDhhmmss>`.
- AMI tags: `cf:os=ubuntu`, `cf:os-family=linux`, `cf:os-version=26.04`, `cf:managed=true`, `cf:poc=true`, `cf:git-sha=<sha>`.
  Snapshots get the same tags.
- Terraform selects the latest bake within the chosen release: `most_recent = true`, `owners = ["self"]`,
  and tags `cf:os=<os>`, `cf:os-version=<imageVersion>`, `cf:managed=true`.
- Baked on the image:
  - `/etc/ssh/trusted_user_ca_keys` contains the CA pub key.
  - An sshd drop-in sets `TrustedUserCAKeys /etc/ssh/trusted_user_ca_keys`,
    `AuthorizedPrincipalsFile /etc/ssh/auth_principals/%u` and `PasswordAuthentication no`.
  - `/etc/ssh/auth_principals/<login user>` (`ubuntu`) contains the lines `cf-automation` and `team-platform-admins`.
  - `/etc/compute-factory/image-info.json` holds image name, build date and git SHA.
  - Base packages are `curl`, `jq`, `unzip` only. Do **not** bake anything from the software catalog.
- The bake workflow reads the CA pub key from SSM `/compute-factory/ssh-ca-pub` at build time. It is never committed.
- Azure: base is Canonical's Marketplace image `Canonical:ubuntu-26_04-lts:server:latest` (Gen2, x64,
  no plan). Packer publishes gallery version `1.<YYYYMMDD>.<hhmmss>` of `cfimages/ubuntu` (shallow
  replication) with the same `cf:*` tags, after `waagent -force -deprovision+user`. The build VM is
  `Standard_D2ls_v6` with the NVMe disk controller. `ubuntu` is the definition for Ubuntu 26.04 only.
  Terraform launches the latest bake in the definition mapped to the chosen release (§7).
- The principals file is named after the OS login user (`platform.json` `os.<os>.sshUser`; Packer passes
  `cf_ssh_user`).

## 3. SSH identity

| Principal | Meaning |
|---|---|
| `cf-automation` | pipeline (fry). Cert validity 1h, key ID `gha-<run_id>` |
| `team-<group>` | a member of Backstage group `<group>`, e.g. `team-alpha` |
| `team-platform-admins` | admins; baked into every VM |

- Browser terminal (§8.4) certs: a fresh per-session ed25519 key generated on the IDP host, key ID
  `idp-terminal:<username>`, one principal (`team-<VM's team>` if the user is in it, else
  `team-platform-admins`), validity 5 minutes (only the SSH handshake needs it). The private key never
  leaves the IDP and is deleted when the session ends.

- OS login user: `platform.json` `os.<os>.sshUser` of the VM's platform (`ubuntu` for Ubuntu on AWS and
  Azure); recorded as `cf/ssh-user`.
- The fry stage `access` writes the **whole** `/etc/ssh/auth_principals/<login user>` file with exactly
  three lines: `cf-automation`, `team-platform-admins` and `team-<owning team>`, and the drop-in
  `/etc/ssh/sshd_config.d/01-compute-factory-certificates-only.conf` (`AuthorizedKeysFile none`), so
  the admin key Azure requires at launch stops working. Then it runs `sshd -t` and reloads.
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
| `stage` | `provision` | `register` \| `provision` \| `baseline` \| `software` \| `ad-join` \| `access` \| `configure` \| `start` \| `stop` \| `reboot` \| `os-update` \| `destroy` \| `finalize` |
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
- Azure: job env `ARM_USE_OIDC=true` and `ARM_CLIENT_ID`/`ARM_TENANT_ID`/`ARM_SUBSCRIPTION_ID` from the
  `AZURE_*` variables (Terraform azurerm); `azure/login@v3` (OIDC) runs after AWS credentials when the
  validated platform is `azure` and the stage is `provision`, an eligible `destroy`, `start`, `stop` or
  `reboot`. `Validate inputs` outputs `platform` and `ssh_user` (from `platform.json`) for the later steps.
- `permissions: { contents: write, id-token: write }`. Executor code (scripts, Ansible, Terraform) comes
  from the dispatched commit (`github.sha`). Inventory is read from, and committed to, a separate
  checkout of current `main` taken when the job starts, because a stage queued behind another one must
  see that stage's inventory commits. Inventory commits keep the `pull --rebase` retry loop.
- `params_json` per stage:
  - `register`: `{"spec": <Resource spec, §7>}`;
   - `configure`: `{"compute": {platform, os, imageVersion?, size | instanceType}, "configuration": {"software": [...], "adJoin": bool}}`
     (Modify: the complete new desired compute and configuration; platform, OS and image version must not change);
  - `software`: `{}` or `{"remove": ["nginx", ...]}` (catalog packages deselected by Modify, at most 20);
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
| `provision` | `cf/status: provisioning`; `terraform apply`; record outputs; `cf/status: frying`. In a Modify run it applies a size change in place (EC2 stops and starts the instance and the public IP changes; Azure resizes, keeping its static IP). Never a second instance for the same incarnation: the image is ignored after creation, untracked resources of this incarnation are adopted into state by the platform's `adopt.sh` (AWS: instance/SG tagged `cf:vm=<name>` and `cf:request-id=<request_id>`; Azure: the fixed-name resources in `cf-vm-<name>`, whose tags must match), and a stale state lock cannot block retries indefinitely. |
| `baseline`, `software`, `ad-join`, `access` | `cf/status: frying`; read host and SSH user from inventory (an empty `cf/public-ip`, i.e. a stopped VM, fails validation); run the stage playbook (§6.3) with variables derived from the persisted spec. |
| `configure` | Replace `spec.compute` and `spec.configuration` with `params_json` (platform/OS unchanged) through `spec.py`; other spec sections and provenance are kept. No VM contact. |
| `software` (removal) | Besides installing `cf_software`, removes `params_json.remove` minus anything still desired (`apt` `state: absent`; data and configuration files are kept). |
| `ad-join` (leave) | With `adJoin: false` in the persisted spec (Modify turned it off) the mock join record and banner line are removed. |
| `start`, `stop`, `reboot` | Instance from `cf/instance-id` (matching the platform's `instanceIdPattern`); it must carry `cf:vm=<name>`, `cf:request-id=<request_id>`, `cf:poc=true`. Cloud calls come from `terraform/platforms/<p>/power.sh`. `stop`: stop and wait (Azure: deallocate, also for a guest-stopped VM that is still allocated), then `cf/public-ip: ""`. `start`: start and wait, record the current `cf/public-ip`, wait for SSH. `reboot`: requires `running`; cloud reboot, then wait until the guest's boot ID changes (10 min). Already in the target state → success. `cf/status` is not changed. |
| `os-update` | Requires a public IP. Runs `ansible/maintain/os-update.yml` (full `apt` upgrade; reboot only if `/var/run/reboot-required`). `cf/status` is not changed. |
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
| `cf_software_remove` | list | `["docker"]` (empty except in a Modify `software` stage) |
| `cf_ad_join` | bool | `true` (desired mock AD membership) |
| `cf_ad_domain` | string | `corp.example.local` |

Stage playbooks: `ansible/fry/baseline.yml`, `software.yml`, `ad-join.yml`, `access.yml`; the
maintenance playbook `ansible/maintain/os-update.yml` (stage `os-update`) takes the same variables.
Install with `pip install -r requirements-ansible.txt`.

## 7. VM inventory (platform repo `vms/<name>/`)

`catalog-info.yaml` is the only per-VM configuration file; `request.yaml` is removed:
```yaml
apiVersion: backstage.io/v1alpha1
kind: Resource
metadata:
  name: demo-1
  title: demo-1
  description: ubuntu VM on aws (S) for team alpha   # e.g. "ubuntu VM on azure (S) …"
  annotations:
    cf/status: provisioning        # provisioning | frying | ready | failed | destroying
    cf/public-ip: ""               # filled after provision and start; "" while stopped
    cf/ssh-user: ubuntu            # platform.json os.<os>.sshUser, written at register
    cf/instance-id: ""             # EC2 instance ID, or the Azure VM's ARM resource ID
    cf/instance-type: ""   # launched instance type / Azure size, set with cf/instance-id after apply
    cf/requested-by: amirasyraf
    cf/requested-at: "2026-11-15T12:00:00Z"
    cf/request-id: "scaffolder-task-id" # VM incarnation; equals the stage.yml request_id input
    cf/ready-at: "2026-11-15T12:03:00Z" # first readiness only; absent until ready
    # cf/failed-at is set on first recorded failure; neither timestamp is reset by later runs
    cf/run-id: "1234567890"        # last stage.yml run that wrote this file
    cf/run-url: https://github.com/amirasyraf/compute-factory-platform/actions/runs/1234567890
spec:
  type: vm
  owner: group:default/alpha
  lifecycle: poc
  compute:
    platform: aws          # a terraform/platforms/<p> directory: aws | azure
    os: ubuntu             # a key of that platform's platform.json os
    imageVersion: "26.04"  # chosen release; absent only for legacy inventory/queued requests
    size: S                # or, instead of size: instanceType: m7i.large / Standard_D2s_v6 (Advanced picker)
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
  `compute.{platform,os}` plus exactly one of `compute.size` (a key of that platform's `sizes.json`:
  XS/S/M/L) or `compute.instanceType` (matching the platform's `instanceTypePattern` and not its
  `excludedInstanceTypePattern`: AWS `m7i.large`, `mac*` rejected; Azure `Standard_*`). Supported
  platforms are the `terraform/platforms/<p>` directories with `platform.json` and `sizes.json`;
  supported OSes are the keys of that file's `os`. Owner normalizes to
  `group:default/<team>`; other namespaces/kinds are rejected. Optional `lifecycle` defaults to `poc` (bounded
  nonempty string); `configuration` defaults to `{software: [], adJoin: false}`; billing/tags to `{}`.
  Software is a unique list of software-catalog keys; AD join is a boolean, not an encoded string.
  Unknown section fields, invalid types and control characters are rejected. Custom fields are not
  automatically schema-validated by the stock Backstage catalog.
- Billing supports only nonempty `costCenter` and `projectCode` strings up to 256 characters,
  projected as cloud tags `cost_center` and `project_code` (VMs launched before this change carry
  `billing:cost-center`/`billing:project-code`). The vm-request form (`tags` parameter, `ui:field:
  VmTagsEditor`: one table with fixed `cost_center`/`project_code` rows plus optional tags) and the IDP
  dispatch endpoint require both for new requests; `spec.py` keeps them optional so older specs still
  load. Values are self-reported, not trusted billing authorization. `cloudTags` is at most 40 string
  entries (keys 1–128 characters, values 0–256); `Name`, `cost_center`, `project_code` and prefixes
  `cf:`, `billing:` plus the platform's `reservedTagPrefixes` (AWS `aws:`; Azure `azure`, `microsoft`,
  `windows`) are reserved, case-insensitively, and keys may not contain the platform's
  `forbiddenTagKeyCharacters` (Azure `<>%&\?/`).
  Mandatory platform tags cannot be overridden. Never store secrets in catalog data or tags.
- `terraform/platforms/<p>/platform.json` (read by `spec.py`, `validate.sh`, `inventory.sh`, Terraform
  and the IDP from `main`): `label`, `os` (`{<os>: {label, versions, sshUser, family}}`),
  `instanceTypePattern`, `excludedInstanceTypePattern` (`""` = none), `instanceIdPattern`,
  `reservedTagPrefixes`, `forbiddenTagKeyCharacters`, `staticPublicIp` (AWS `false`: stop releases the
  address and resize/start gives a new one; Azure `true`: kept; the IDP's stop/start wording uses it).
   `os.<os>.versions` lists launchable release strings (`^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$`).
   Publish the bake before advertising a release. On Azure `os.<os>.galleryImages` maps each release to a
   distinct gallery definition; retain mappings for existing VMs even after a release is no longer
   advertised. Keep the AWS golden image available for Terraform to resolve during existing VM operations.
   `family` selects the software catalog column. The register stage rejects explicit releases not offered
   on the platform. Legacy inventory and queued requests without a release retain the Ubuntu 26.04 fallback.
- Terraform loads persisted spec and maps it to temporary `.tfvars.json` (`name`, `os`, `image_version`, `size`,
  `team`, original `requested_by`, `cloud_tags` including billing). It deletes that file on exit.
  Each stage derives Ansible variables and applicability from the committed entity.
- Modify persists the new desired compute and configuration (`configure`) before applying it: the
  software list is replaced (unchecked catalog software is uninstalled), `adJoin` can be turned on or
  off, and size/instance type can change. Platform, OS, image release, owner, billing, tags and original request
  provenance are preserved. Power state is not desired state: it is observed (§8.3), never stored in
  spec.
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
- **Launch** (IDP `/launch`; `/create` still works): the IDP product registry groups registered products
  into categories and maps each product to its catalog resource type and a tagged Scaffolder template.
  **Compute → Virtual Machines** maps to `Resource` / `spec.type: vm` and `template:default/vm-request`
  (`product` tag, `cf/product: Virtual Machines`, `cf/launch-flow: platform-image`). VM platform cards
  (`/launch/compute/virtual-machines`, from `GET /api/vm-sizes/platforms`) lead to image release cards
  (`/launch/compute/virtual-machines/<platform>`). Continue opens
  `/create/templates/default/vm-request?formData={"compute":{"platform":…,"os":…,"imageVersion":…}}`.
  Admission requires a release. For a client that omitted it, the IDP may infer the only
  configured release for that OS/platform; ambiguous input is rejected. Old `/launch/vm-request[/<platform>]`
  links redirect. Home groups visible catalog Resources by category/product; the VM product page is
  `/products/compute/virtual-machines`, and `/catalog` remains the full inventory.
- Template names (`metadata.name`): `vm-request`, `vm-modify` (platform repo) and
  `ssh-cert` (idp repo). VM templates only **create an orchestrator run** and then finish. The whole
  pipeline, including non-VM steps such as the mock ServiceNow request, is an orchestrator run (§8.2):
  live progress, retry and skip are on the IDP run page (`/vms/runs/<id>`) and the VM entity page. When
  a VM task completes with a run, the IDP task page takes the browser to that run page.
  - `vm-request` has one step, `dispatch`, action `cf:vm:dispatch`, inputs `{name, specJson}` (unchanged
    spec JSON construction); outputs `{runId: number, runUrl: string}`. It calls `POST /api/vms/requests`
    with the initiating user's credentials; admission reserves demo quota and creates the provision run.
    `request_id` is the task ID, so a Scaffolder retry or recovery returns the same run.
    Template output links: `Follow Provisioning` → `${{ steps.dispatch.output.runUrl }}`, and
    `Open VM in Catalog` → `resource:default/<name>`.
  - `vm-modify` step `run`, action `cf:vm:run`, inputs `{operation: modify, vm: <entity ref>, compute,
    software, adJoin}`. Outputs `{runId, runUrl}`; link `Follow run` → `runUrl`. The VM page opens it
    with `?formData=` holding the VM ref and its current `compute` and `configuration`; the VM field is
    read-only (`EntityPicker`, `catalogFilter: { kind: Resource, spec.type: vm }`), `compute` uses
    `ui:field: VmComputePicker` with `ui:options: { locked: true }` (platform and image shown without a
    "Change" link), software is the catalog checkboxes, AD join a boolean.
  - `VmComputePicker` shows the chosen platform and image (no dropdowns) with a "Change" link back to
    Launch, then size cards and the Advanced picker for that platform. Platform enum is `[aws, azure]`;
    the platform definition validates OS/release; `instanceType` pattern is the union of the platforms' patterns.
  - `cf:vm:run` input `operation` is one of `modify | reapply | start | stop | reboot | update | destroy`.
    It calls `POST /api/vms/runs` (§8.2) with the task ID as request key. Start, stop, reboot, OS updates,
    re-apply and destroy are started from the VM page directly (same endpoint), not from templates.
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
  GHA inputs or VM inventory. Day-2 runs do not call it.

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
  written only after orchestration gives up) if never ready. Day-2 runs (including stop) do not extend lifetime. Disable
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
cancels any other active run for that VM; any other new run cancels a failed day-2 run (every kind
except `provision`). Any other creation while a run is active is rejected (409): a running run, or a
failed (incomplete) provision, must be retried, skipped or destroyed first.

| Run kind | Stages, in order |
|---|---|
| `provision` | `servicenow`, `register`, `provision`, `baseline`, `software`, `ad-join`, `access`, `finalize` |
| `modify` | `configure`, `provision` (if size changed), `software` (if the list changed), `ad-join` (if AD join changed), `finalize` |
| `reapply` | `baseline`, `software` (if any), `ad-join` (if enabled), `access`, `finalize` |
| `power` | one of `start`, `stop`, `reboot` (no `finalize`) |
| `update` | `os-update` (no `finalize`) |
| `destroy` | `destroy`, `finalize` (`finalize` runs only on the failure path) |

- Runs without `finalize` (power, OS updates) never write `cf/status`; when their stage fails for good
  the run is `failed` directly.

- Stages run in one of two places. `servicenow` runs inside the IDP (an in-process handler); every
  other stage is a `stage.yml` dispatch (§6). In-process stages have no GitHub run (`runUrl: null`,
  empty `steps`); an HTTP 5xx, 429, timeout or network error is transient, any other unexpected
  response is permanent. An in-process attempt interrupted by a restart is simply run again (handlers
  are idempotent per request ID).
- Not-applicable stages (no software selected, no AD join, Modify stages for unchanged parts, destroy
  `finalize` on success) are `skipped` with `skippedBy: null`.
- Stage statuses: `pending | running | retrying | success | failed | skipped | cancelled`.
  Run statuses: `running | failed | succeeded | cancelled`.
- Automatic retry: at most 4 attempts per stage (1 + 3 retries), waiting 30 s, 2 min and 8 min before
  attempts 2–4 (`retrying` while waiting). An attempt that has not completed 45 min after dispatch is
  failed (and its GitHub run cancelled, best effort). Permanent failures (§6.1, and dispatch rejections
  other than 429/5xx) fail the stage immediately.
- When a stage fails for good, the orchestrator runs `finalize` with `outcome: failed` (same retry
  policy) and then sets the run to `failed`. If `finalize` itself fails, the run is `failed` without a
  further failure record. Success of every stage ends with `finalize` `outcome: ready` (provision,
  modify, reapply) and run `succeeded`.
- Manual actions, allowed only while the run is `failed`, for `platform-admins` and members of the run's
  team:
  - **retry** resets the failed stage to a fresh attempt budget (and `finalize` to pending), then
    continues the run from that stage;
  - **skip** marks the failed stage `skipped` (`skippedBy` = user) and continues with the next stage.
    Only `software` and `ad-join` of provision and reapply runs are skippable (a Modify has already
    persisted its change, so skipping would hide it). The desired spec is not changed by a skip.
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
| `GET /summary` | `{ observedAt, observedError, vms: [{ name, latest: RunView, observed?: ObservedVm \| null }] }`: the newest run per VM name visible to the caller, with the health check of that VM incarnation (§8.3). `observed` is absent until the VM's platform has completed a scan; `observedAt` is the oldest configured platform's last scan |
| `POST /runs` | `{operation, vm, requestKey, compute?, software?, adJoin?}` → `{runId, runUrl}`. 400 invalid, not the platform's instance type, or (Modify) nothing to change, 403 other team or demo-disallowed instance type, 404 unknown VM, 409 conflict, 503 automation off or platform/size files unreadable |
| `GET /observed/:name?platform=<p>` | `{ enabled, checkedAt, error, vm: ObservedVm \| null }` (§8.3); `checkedAt`/`error` are that platform's (all platforms without `platform`) |
| `POST /terminal` | `{vm, cols, rows}` → `{id, host, user}` (§8.4); 409 not running / no IP, 429 two sessions already open, 503 automation off |
| `GET /terminal/:id/output?after=<seq>` | `{seq, data, closed: string \| null}`; waits up to 25 s for output after `seq` |
| `POST /terminal/:id/input` | `{data}` (≤ 16 KiB keystrokes) or `{cols, rows}` (resize) → `{}` |
| `DELETE /terminal/:id` | Close the session → `{}` |
| `POST /runs/:id/retry` | `RunView`; 409 when not retryable, 403 when not allowed |
| `POST /runs/:id/skip` | `RunView`; 409 when the failed stage is not skippable or the run is not failed |

`POST /runs`: `operation` maps to a run kind (`start`/`stop`/`reboot` → `power`, `update` → `update`,
otherwise the same name). The VM entity is read with the caller's credentials; its `cf/request-id` is
the run's request ID and its owner group the run's team. `requestKey` (`^[a-zA-Z0-9_-]{1,100}$`; a
Scaffolder task ID or a browser UUID) makes a repeated submit return the first run. Modify sends the
  complete new `compute` (`{platform, os, imageVersion?, size | instanceType}`, platform/image unchanged), `software`
(catalog keys) and `adJoin`; the backend diffs them against the entity spec to plan the run, and
computes the packages to remove. While the VM platform's health check is fresh (under 2 minutes), it refuses (409)
what cannot work: any operation but destroy when no instance of this incarnation exists, `start` on a
running VM, `stop` on a stopped one, and everything else unless the VM is running.

```ts
type RunView = {
  id: number;
  kind: 'provision' | 'modify' | 'reapply' | 'power' | 'update' | 'destroy';
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

### 8.3 VM health check (`vms` backend plugin)

Power state is observed, not desired: it is never stored in spec or inventory annotations (only
`cf/public-ip` follows start/stop). Where `computeFactory.demoAutomation` is on, the plugin scans every
platform every 30 seconds and right after any non-provision run finishes, keeping each platform's
result separately (one failing keeps its previous result and never affects the others):
- AWS: `ec2:DescribeInstances` (filter `tag:cf:poc=true`, states pending/running/stopping/stopped/
  shutting-down) and `ec2:DescribeInstanceStatus` for running instances. The IDP host role
  (`infra/idp.tf`, policy `observe-vms`) allows only these two read-only calls.
- Azure (only when `AZURE_READER_SECRET_ID` is set; otherwise its status error is "Azure health check
  not configured" and Azure VMs are never refused on its account): with the Reader principal, ARM
  list VMs (Compute `2026-04-01`, plus `statusOnly=true`), public IPs (Network `2026-01-01`) and
  Resource Health availability statuses (`2026-07-01`) of the subscription. Power state `starting` →
  pending, `running`, `stopping`/`deallocating` → stopping, `stopped`/`deallocated` → stopped;
  provisioning `Deleting` → shutting-down. Health (running only): `Available` → ok,
  `Unavailable`/`Degraded` → impaired, `Unknown` → unknown, none yet → initializing. The public IP
  is the one tagged with the same `cf:vm` and `cf:request-id`.
The result is kept in memory, keyed by the `cf:vm` tag (the live instance wins over one still
shutting down).

```ts
type ObservedVm = {
  platform: string;            // aws | azure
  instanceId: string;          // EC2 instance ID or Azure VM ARM ID
  state: string;               // pending | running | stopping | stopped | shutting-down
  health: 'ok' | 'impaired' | 'initializing' | 'unknown' | null; // cloud status checks; null unless running
  instanceType: string | null;
  publicIp: string | null;
  requestId: string | null;    // cf:request-id tag; consumers ignore another incarnation
  team: string | null;         // cf:team tag; /observed/:name shows the VM only to that team and admins
  launchedAt: string | null;
};
```

- `checkedAt: null` means no scan has completed yet; `error` is the last scan's failure (the previous
  result is kept). `enabled: false` (local development) means no health check runs.
- A VM whose catalog status is `ready` but whose incarnation has no instance is shown as "Instance not
  found" ("No instance … found in <platform>" in messages); a stopped one as "Stopped". A console stop or start is picked up within 30 seconds; the
  inventory `cf/public-ip` is then stale until the next start/stop run, so the UI prefers the observed
  address.
### 8.4 Browser terminal (`vms` backend plugin)

- "Open terminal" on the VM page opens an SSH session from the IDP host to the VM: the backend checks
  access exactly like `POST /runs` (team or admin; entity read with the user's credentials), refuses a VM
  the fresh health check does not show running, and connects to the observed public IP (else
  `cf/public-ip`). It signs a per-session key (§3) and runs the system `ssh -tt <cf/ssh-user>@<ip>` in a PTY
  (`node-pty`); 409 when `cf/ssh-user` is missing or not a valid user name. Host keys are not pinned
  (POC, same as the fry stages).
- Transport is plain HTTP, not WebSocket (TLS-inspecting proxies hold back long-lived streams): the
  browser (xterm.js) posts keystrokes in order and long-polls output (25 s). Sessions belong to the user
  who opened them; other users get 404.
- Limits: 2 open sessions per user; closed after 15 minutes without input, 90 s without an output poll
  (tab closed), or when `ssh` exits. Output kept per session: the last 256 KiB. Sessions live in memory,
  so a backend restart ends them.
- Audit: backend log lines `Terminal <id> opened: <user> → <vm> (<ip>) as <principal>` and
  `Terminal <id> closed: … after <n>s: <reason>`; the VM's auth log shows the cert key ID. Keystrokes and
  output are not recorded.
- Only where `computeFactory.demoAutomation` is on (production); otherwise 503.

## 9. IDP runtime (orchestrator infra ↔ Agent C deploy)

- EC2: Ubuntu 26.04 **arm64** (t4g.medium). Cloud-init installs Docker Engine + compose plugin, puts
  `ubuntu` in the `docker` group, and creates `/opt/idp` owned by `ubuntu`. IMDSv2 has hop limit 2.
- SSH: `ssh -i ~/.ssh/id_ed25519 ubuntu@<ip>` (EC2 key pair from that key).
- `compute-factory-idp/deploy/build.sh` builds and type-checks a local `linux/arm64` image, tags it
  `compute-factory-idp:latest`, and prints the full `sha256:` image ID on stdout (logs and timings on
  stderr). `IDP_IMAGE=<image-id>` pins the local smoke check to that image.
- `compute-factory-idp/deploy/deploy.sh <host> <env-file> [<image-id>]`:
  - ships a supplied local IDP image directly; if omitted, builds first using `deploy/build.sh`;
  - copies the image's packaged `docker-compose.yml` and `Caddyfile`, plus `<env-file>` as `.env`, to `/opt/idp`;
  - starts the host-local `registry` service (`127.0.0.1:15000` on the host only) and pushes the
    image to it through an SSH tunnel (only missing layers transfer);
   - verifies the pulled and deployed Backstage image IDs match the local image and brings the stack
     up. Fresh Caddy reads its config at startup; a reused running Caddy reloads it with bounded
     retries. Garbage collection is explicit via `./poc prune-idp-images`
    (`deploy/deploy.sh --prune <host>`), not part of rollout.
  It must be idempotent and must not prompt.
- `./poc up` configures DNS and restores TLS storage as needed for bootstrap. `./poc deploy-idp
  [<image-id>]` reads the existing host and environment values from local Terraform state, leaves
  DNS and TLS storage alone, and verifies readiness. Both generate these `.env` keys:

| Key | Source |
|---|---|
| `IDP_DOMAIN` | `idp.amirasyraf.dev` |
| `IDP_USERS_B64` | base64 of `compute-factory/.poc-users` (`username:hash` lines; hash from `compute-factory-idp/deploy/hash-password.mjs`) |
| `GITHUB_TOKEN` | user's fine-grained PAT (integration: catalog reads + workflow dispatch) |
| `POSTGRES_PASSWORD` | random |
| `BACKSTAGE_TOKEN` | random (static external access token) |
| `AWS_REGION` | `ap-southeast-2` |
| `SSH_CA_SECRET_ID` | `compute-factory/ssh-ca` |
| `AZURE_READER_SECRET_ID` | `compute-factory/azure-reader` (unset: Azure specs, picker and health check report "not configured") |

- Caddy terminates TLS for `IDP_DOMAIN` and proxies to the backstage container on port 7007.
  It rewrites only `/mock-rest/servicenow` to the authenticated `/api/mock-rest/servicenow` plugin route;
  other paths are unchanged. No new environment variable or external-access token is introduced.
- Health URL used by `./poc up`: `https://idp.amirasyraf.dev/.backstage/health/v1/readiness` must return 200.
  The deployment also confirms it can read catalog locations and submit authenticated refresh requests.

## 10. Terraform platform contract (`terraform/platforms/<p>`: `aws`, `azure`)

- Each platform directory has the Terraform root, `platform.json` (§7), `sizes.json`, `adopt.sh <vars-file>
  <tracked-addresses>` (adopt untracked resources of this incarnation; fail on ambiguity) and
  `power.sh` (sourced; `vm_describe` → `{state, ip, tags}`, `vm_start`, `vm_stop`, `vm_reboot`).
- Backend: `backend "s3" {}` with partial config passed at init (also for Azure: state stays in AWS):
  `-backend-config=bucket=$TF_STATE_BUCKET -backend-config=key=vms/<p>/<name>/terraform.tfstate -backend-config=region=ap-southeast-2 -backend-config=use_lockfile=true`.
- Variables: `name` (required), `os` (default `ubuntu`), `image_version` (null defaults to the legacy
  release `26.04`), `size` (default `S`), `instance_type` (default
  `""`; overrides `size`), `team` (default `""`),
  `requested_by` (default `""`), `request_id` (default `""`; VM incarnation, tagged as `cf:request-id`),
  `cloud_tags`; Azure also `location` (default `australiaeast`).
  **`terraform destroy -var name=<name>` must work with nothing else**,
  because `./poc down` uses that.
- Outputs: `public_ip`, `ssh_user` (from `platform.json`), `instance_id`, `instance_type`.
- Size map: `terraform/platforms/<p>/sizes.json`, size (`XS`/`S`/`M`/`L`) to instance type. AWS:
  `t3.micro`/`small`/`medium`/`large`. Azure: `Standard_D2ls_v6`/`D2s_v6`/`D4ls_v6`/`D4s_v6`. `spec.py`
  and Terraform read it; the IDP reads the same files from `main` (§10a). Modify uses the same field.
  AWS Terraform rejects types that cannot boot x86_64 and launches into a default subnet whose AZ offers
  the type. Azure: an admin key (`tls_private_key`, only in state) is required at launch and disabled by
  the `access` stage; the VM ignores image and admin-key changes; the disk controller is Azure's default
  for the size and image (NVMe on v6).
- Demo mode: while on, the IDP accepts `compute.instanceType` only from the union of every platform's
  `sizes.json` values (`/api/vms/demo/state` returns them as `instanceTypes`; empty, and explicit types
  refused with 503, when the files cannot be read), for requests and Modify; sizes are allowed.
- A changed `size`/`instance_type` is an in-place update (stop, modify, start); the provision plan still
  refuses any delete or replacement.
- Resources are tagged `cf:poc=true`, `cf:vm=<name>`, `cf:team=<team>`, and `cf:request-id=<request_id>`
  when set. No key pair (the CA is used). The instance ignores AMI changes after creation, so a retry after
  a newer bake never replaces it.
- `./poc down` runs, for each key under `vms/<p>/*/terraform.tfstate`:
  initializes the S3 backend from the canonical platform `main` checkout and runs
  `terraform destroy -auto-approve -var name=<name>` (Azure with the laptop's `ARM_*` service principal).
  Only successful destroys remove the corresponding S3 state object. Outstanding or unverified VM
  state keeps the bucket and golden images available for recovery.

### 10a. IDP `vm-sizes` API (signed-in users)

- `GET /api/vm-sizes/platforms` → `{platforms: [{id, label, available, os: [{id, label, versions}],
  reservedTagPrefixes, forbiddenTagKeyCharacters, staticPublicIp, error}]}`: `aws`, `azure` from `platform.json` on
  `main` (cached 5 minutes), plus `{id: vsphere, label: vSphere, available: false}` ("Coming soon").
- `GET /api/vm-sizes/sizes?platform=<p>` (default `aws`) → `{platform, label, reservedTagPrefixes,
  sizes: [{size, instanceType, specs}], error}`. Specs: AWS `ec2:DescribeInstanceTypes`; Azure the
  Resource SKUs API (`2021-07-01`, location filter) with `quotaOk`.
- `GET /api/vm-sizes/instance-types?platform=<p>` → `{region, types}`; each type `{type, family,
  category, currentGeneration, bareMetal, gpus, vcpus, memoryMiB, architectures, network, burstable,
  quotaOk}`. AWS: every x86_64, EBS-backed type offered in the region (`quotaOk: null`). Azure: VM SKUs
  without a `Location` restriction, `CpuArchitectureType=x64`, `HyperVGenerations` containing `V2`;
  category from the series letter; `currentGeneration` always true; `quotaOk` = free family and regional
  `cores` usage (Compute usages, cached 5 minutes) ≥ vCPUs, null when usages are unavailable. 503 when
  the list is unavailable.

## 11. Ground rules for all agents

- Only touch your own repo or paths. Never run `./poc down`, `terraform destroy` on infra you didn't create,
  or anything against other AWS resources in the account.
- Every AWS resource you create, including test ones, carries `cf:poc=true`. Clean up your own test resources.
- Long-running commands (builds, `yarn install`, waiting on GHA runs) use the shell tool's background mode,
  not `nohup`/`&`, and no sleep-polling. `gh run watch` in background mode is fine.
- No secrets in Git. `.env` files are gitignored.
