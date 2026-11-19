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
| OS types | Linux, Windows | Ubuntu Server 26.04, Windows Server 2025 |
| Platforms | AWS, Azure, vSphere | AWS and Azure |
| Fry stages | baseline, software, AD join, ... | baseline, software, AD join (mocked), access |

Guiding rules: **KISS**. Every OS- or platform-specific piece sits behind a directory or
variable boundary. A platform is
its `terraform/platforms/<p>/` directory in the platform repo: `platform.json` (label, OSes with their
login user, OS family, display name, versions and memory minimum, instance-type/ID patterns, reserved
tag rules), `sizes.json` (per OS family),
the Terraform root, `adopt.sh` (recovery) and `power.sh` (start/stop/reboot). Scripts, `spec.py`,
Terraform and the IDP read those files; outside them only a few places name a platform: the
workflow's cloud login step, the bake's source selector, the IDP's per-cloud spec/health readers and
`./poc` sweeps.

The IDP organizes products independently of the cloud where they run. Home groups visible resources
by category and product; Resources stays a searchable inventory. **Launch** starts at product
categories. **Compute → Virtual Machines** leads to platform and image release cards, then the VM form (5.2).

Out of scope for the POC: real AD, Vault, HA, production-grade security, cost controls,
image lifecycle cleanup, and a GHA pipeline to deploy the IDP.

## 2. Fixed decisions

| Item | Decision |
|---|---|
| AWS account | `amirasyraf` profile, `134584031874` (one AWS account for all AWS resources; Azure is in section 2's Azure rows) |
| Region | `ap-southeast-2` (Sydney) |
| GHA -> AWS | Existing OIDC role `arn:aws:iam::134584031874:role/github-aws-role` (trusts `repo:amirasyraf/*`). No static keys. |
| Repos (user `amirasyraf`; private except `compute-factory-platform` and `compute-factory-builder`, public for free Actions minutes and checked for committed secrets first) | `compute-factory` (this doc, `./poc`, shared AWS infra), `compute-factory-idp`, `compute-factory-builder`, `compute-factory-platform` |
| Lifecycle | `./poc up` creates everything; `./poc down` removes everything, including user VMs (section 12) |
| Tagging | Every AWS resource, from Terraform, Packer or anywhere else, gets the tag `cf:poc=true`; `down` checks that none remain |
| IDP | Backstage (latest, v1.56.x at time of writing) on a single EC2, Docker Compose, Caddy TLS, `idp.amirasyraf.dev` |
| IDP deploy | Manual script from the laptop. No GHA. |
| Base image | Azure: Canonical's Marketplace image `ubuntu-26_04-lts/server` (section 4.1). AWS: Canonical's official Ubuntu AMI, looked up through the public SSM parameter `/aws/service/canonical/ubuntu/server/<ver>/stable/current/amd64/hvm/ebs-gp3/ami-id`. These are the same official images the Marketplace lists, without needing a Marketplace subscription. |
| Bake trigger | GHA in the builder repo, on push to `main` |
| Provision/Fry engine | GHA in the platform repo, triggered by Backstage |
| Software install | `apt` on Ubuntu; winget (the `Microsoft.WinGet.Client` PowerShell module, as SYSTEM) on Windows. Same role interface. |
| VM access | SSH for every OS and platform, authenticated with SSH user certificates signed by a mock CA. Windows adds Remote Desktop, only through SSH port forwarding (browser RDP via guacd, or a native client), with one shared login per VM |
| Network | Default VPC in ap-southeast-2 (`vpc-23619045`, exists); VMs get public IPs; SG allows 22 from anywhere (POC) |
| Azure | Subscription `amirasyraf` (`74b39a79-…`), tenant `c26545da-…`, region `australiaeast`. One resource group, VNet, NSG (22 from anywhere) and static public IP per VM |
| Shared base for Azure | AWS stays the shared base: the SSH CA (Secrets Manager), Terraform state (S3) and the IDP host. Azure stages log in to both clouds |
| GHA -> Azure | Entra app `cf-github-actions` with federated credentials for the builder and platform repos on `main` (immutable-subject format); Contributor on the subscription. No secrets |
| IDP -> Azure | Reader service principal `cf-idp-azure-reader` (certificate, 2-year); its JSON credential is in Secrets Manager `compute-factory/azure-reader`, read by the IDP host role |

## 3. Architecture

```
 User (browser)                                 User (terminal)
     |  0. "Get SSH certificate" -> cert (instant)    |  ssh ubuntu@<ip>   (key + user cert)
     v                                                v
 idp.amirasyraf.dev  (EC2: Caddy -> Backstage -> Postgres)        VM (EC2 or Azure, golden image)
     |  1. Launch cards -> template form -> cf:vm:dispatch (quota)   ^
     |  2. provision run in the `vms` orchestrator: ServiceNow mock  |
     |     (in the IDP), then one stage.yml dispatch at a time --+  |  terraform / ansible over SSH
     |     (retries, retry/skip, run state in its DB)            |  |  (pipeline uses its own short cert)
     |  5. reads catalog (vms/*/catalog-info.yaml)               |  |
     v                                                           v  |
 GitHub: compute-factory-platform                         GHA runner (OIDC -> AWS, + Azure)
   - workflow: stage.yml (one stage per dispatch)            3. terraform apply (S3 state)
   - vms/<name>/  (inventory, written by stages)             4. fry stages (ansible), commit status
                                                                to vms/<name>/, ping Backstage refresh
 GitHub: compute-factory-builder
   - push to main -> packer + ansible -> golden AMI / gallery version -------^ (Terraform looks it up)
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
decisions derive from that entity too. Modify persists the new size, software list and AD join
before applying only what changed; unchecked software is uninstalled. Observed-status writes never
alter desired spec, and power state (running/stopped) is observed by the IDP health check, never
stored as desired state. Editing Git alone does not trigger an apply.

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
    source.aws.pkr.hcl         # amazon-ebs source
    source.azure.pkr.hcl       # azure-arm source -> Azure Compute Gallery cf-images/cfimages/ubuntu
    # source.vsphere.pkr.hcl   # vsphere-iso/clone (future)
  windows/                     # Windows Server (images.yaml version = os_version, e.g. 2025)
    windows.pkr.hcl            # WinRM communicator; EC2Launch reset (AWS) / sysprep (Azure)
    source.aws.pkr.hcl source.azure.pkr.hcl winrm.ps1   # winrm.ps1: EC2 user data for the build's WinRM
ansible/
  bake.yml                     # entry playbook, roles chosen by OS family
  roles/
    common/                    # Linux: updates, base packages; Windows: UTC, RDP allowed, WinGet module
  connection/<family>.yml      # become (Linux) / WinRM settings (Windows), passed by Packer
    ssh_ca/                    # installs CA pubkey + sshd TrustedUserCAKeys + AuthorizedPrincipalsFile
                               # CA PUBLIC key is NOT committed: bake reads it from SSM parameter
                               # /compute-factory/ssh-ca-pub (the CA lives as long as the images; 12.1)
images.yaml                    # build matrix: ubuntu 26.04 and windows 2025, each on aws and azure
.github/workflows/bake.yml     # on push to main + workflow_dispatch (used by `poc up`):
                               # matrix from images.yaml -> packer build -only=<platform>
```

Bake does:
1. Starts from the latest official Ubuntu image: the AMI from the SSM parameter above on AWS,
   Canonical's Marketplace image `ubuntu-26_04-lts/server` (Gen2, x64) on Azure.
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
   - `/etc/ssh/auth_principals/<login user>` (`ubuntu`) contains `cf-automation` (the pipeline) and
     `team-platform-admins`.
   - The config is checked with `sshd -t` before reloading.
4. Produces an AMI named `cf-ubuntu-26.04-<timestamp>` with tags
   `cf:os=ubuntu`, `cf:os-family=linux`, `cf:os-version=26.04`, `cf:managed=true`, `cf:git-sha=<sha>`.
   On Azure, after `waagent -deprovision+user`, a gallery image version `1.<YYYYMMDD>.<hhmmss>` of
   definition `ubuntu` (NVMe-capable: the sizes with quota in the region are NVMe-only), with the same
   tags; Terraform launches the latest bake from the definition mapped to the selected OS release.

Windows Server 2025 bakes the same way from Amazon's `Windows_Server-2025-English-Full-Base` AMI and
`WindowsServer:2025-datacenter-g2` on Azure, over WinRM (Packer's ansible provisioner with `pywinrm`):
OpenSSH Server (in-box) with PowerShell as its shell and a whole `sshd_config` that accepts only CA
certificates (`AuthorizedKeysFile none`, port forwarding allowed for RDP), the principals file for the
login user (`Administrator` on AWS; `cfadmin` on Azure, which forbids `Administrator`), UTC, Remote
Desktop allowed, provenance, and the WinGet PowerShell module for all users. No updates at bake (the
bases are patched monthly). AWS ends with `ec2launch reset`, Azure with sysprep; gallery definition
`windows-2025`. Details: CONTRACTS §2a.

Platform agnosticism: Packer `-only=<source>.<os>` comes from the matrix. Adding a platform means
adding a source file, a matrix row and its case in the workflow's source selector. Ansible roles branch on `ansible_os_family` (`tasks/linux.yml`,
`tasks/windows.yml`).

### 4.2 `compute-factory-platform` (Provision + Fry)

```
terraform/
  platforms/
    aws/                       # root module for one VM on AWS
      main.tf variables.tf outputs.tf sizes.tf sizes.json platform.json adopt.sh power.sh
    azure/                     # same contract on Azure (resource group cf-vm-<name> per VM)
      main.tf variables.tf outputs.tf sizes.json platform.json adopt.sh power.sh
    # vsphere/                 # future
ansible/
  fry/
    baseline.yml               # hostname, timezone, motd, banner
    software.yml               # installs the user's picks from software-catalog.json (apt / winget)
    ad-join.yml                # MOCK: writes /etc/compute-factory/ad-join.json, logs "joined CORP.EXAMPLE"
    access.yml                 # writes the owning team's principal so team members' certs work
  software-catalog.json        # the only software users may pick, with per-OS-family package IDs
  connection/<family>.yml      # Ansible settings known before facts (become / PowerShell shell)
  requirements.yml             # pinned ansible.windows collection
  roles/ ...                   # per-stage roles with tasks/linux.yml and tasks/windows.yml
catalog/
  templates/
    vm-request/template.yaml   # Backstage Software Template: new VM
    vm-modify/template.yaml    # change size, software or AD join (opened prefilled from the VM page)
    # ("Get SSH certificate" template lives in the idp repo; it doesn't touch this repo)
vms/                           # inventory (written by stage runs only)
.github/workflows/
  stage.yml                    # workflow_dispatch: run exactly one stage (dispatched by the IDP orchestrator)
```

**Fry stages (Ansible)**

| Stage | When | What it does |
|---|---|---|
| `baseline` | always | Hostname set to the VM name, `/etc/hosts`, login banner (VM, team, requester, platform), `/etc/compute-factory/vm-info.json` |
| `software` | if any software picked | Installs the picks using `software-catalog.json`; in a Modify run also removes unchecked catalog packages |
| `ad-join` | if picked (mock) | Writes `/etc/compute-factory/ad-join.json` (domain `corp.example.local`, OU, timestamp), adds a banner line, logs "joined CORP.EXAMPLE.LOCAL". No real AD. When Modify turns AD join off, removes them again. |
| `access` | always, last | Writes `team-<owning team>` into the login user's principals file. Linux: turns off `authorized_keys` logins (Azure requires an admin key at launch; only CA certs log in afterwards), then `sshd -t` and reload. Windows: sets the login user's password to the team's shared one (Secrets Manager `compute-factory/vm/<name>`, created once per VM by the workflow) |

Windows equivalents: baseline sets the computer name (cut to 15 characters; one reboot), UTC, the SSH
banner file `C:\ProgramData\ComputeFactory\banner.txt` and `vm-info.json`; the mock AD join writes
`ad-join.json` and a banner line there.

`ansible/maintain/os-update.yml` (stage `os-update`, the "Apply OS updates" action) is maintenance,
not a fry stage: Linux runs a full `apt` upgrade, then reboots only if `/var/run/reboot-required`
exists; Windows installs updates of every category with `win_updates`, rebooting as needed (the job
allows 40 minutes).

**Software catalog** (`ansible/software-catalog.json`). The POC set:

| Key | Label | linux (apt) | windows (winget) |
|---|---|---|---|
| `git` | Git | `git` | `Git.Git` |
| `htop` | htop | `htop` | — |
| `nginx` | nginx | `nginx` | — |
| `docker` | Docker | `docker.io` | — |
| `vscode` | VS Code | — | `Microsoft.VisualStudioCode` |
| `firefox` | Firefox | — | `Mozilla.Firefox` |
| `7zip` | 7-Zip | — | `7zip.7zip` |

- On Linux the stage installs the `linux` names with `apt`. On Windows it uses the
  `Microsoft.WinGet.Client` PowerShell module as SYSTEM in PowerShell 7 (baked; Windows PowerShell 5.1 is
  refused as SYSTEM) (`Install-WinGetPackage -MatchOption Equals
  -Mode Silent -Scope SystemOrUnknown`): the winget CLI is not supported as SYSTEM and fails in SSH/WinRM
  sessions. Chocolatey was rejected: from 2027-01-01 its terms forbid organisations from using the
  community repository directly.
- The request and Modify forms' `VmSoftwarePicker` reads this file (through the IDP) and shows only
  entries offered for the chosen OS's family.
- The IDP (admission, Modify) and the `register` stage's validation reject any name not in the catalog
  for the chosen OS family, so the form can't be bypassed.
- To add or remove software on an existing VM, use **Modify** on the VM page and check or uncheck it.
  Removal uses `apt` `state: absent` (package data and configuration files stay) or `Uninstall-WinGetPackage`.

**Ansible conventions** (bake and fry):
- Every role's `tasks/main.yml` includes `tasks/linux.yml` or `tasks/windows.yml` based on `ansible_os_family`.
- Connection settings that must be known before facts come from `ansible/connection/<family>.yml`
  (`-e`, chosen by the OS family in `platform.json`): Linux `ansible_become: true`; Windows
  `ansible_shell_type: powershell` (SSH as an administrator; SYSTEM tasks use `runas`). Plays do not set `become`.
- Fry connects over SSH on both OSes; the Windows bake uses WinRM (Packer's communicator).
- The Ansible version and Windows collections are pinned, the same in both repos.

**Platform contract** (every `terraform/platforms/<p>/` root has the same interface):

| Inputs | Outputs |
|---|---|
| `name`, `os`, `image_version`, `size` (XS/S/M/L) or `instance_type`, `team`, `cloud_tags` | `public_ip`, `ssh_user`, `instance_id`, `instance_type` |

- Each platform maps sizes in one data file, per OS family. AWS `sizes.json`: Linux XS=`t3.micro`,
  S=`t3.small`, M=`t3.medium`, L=`t3.large` (2 vCPU, 1/2/4/8 GiB); Windows XS=`t3.medium`, S=`t3.large`,
  M=`t3.xlarge`, L=`t3.2xlarge` (4/8/16/32 GiB). Azure: Linux `Standard_D2ls_v6`, `D2s_v6`, `D4ls_v6`,
  `D4s_v6`; Windows `D2ls_v6`, `D2s_v6`, `D4s_v6`, `D8s_v6` (B-series has no quota in this
  subscription; the region allows 10 vCPUs). Windows needs at least 4 GiB (`minMemoryMiB`): the
  Advanced picker greys out smaller types and AWS Terraform refuses them; the AWS root volume is 50 GiB
  for Windows (`rootVolumeGiB`).
  Regular users pick a size card showing the instance type and live specs from the VM's cloud. A
  collapsed **Advanced** picker lists every x86_64 instance type the platform offers in its region
  (AWS: EC2 offerings; Azure: Resource SKUs without a location restriction that boot the Gen2 image),
  segmented by family type and grouped by family, with search, family/vCPU/memory filters and
  sortable columns (AWS also a current-generation toggle; Azure has no such flag). On Azure, sizes
  without enough free vCPU quota (family and regional) are greyed out as "Over vCPU quota". The choice
  is stored as `compute.instanceType`. Demo mode greys out everything except the types some platform's
  `sizes.json` maps a size to, and the IDP backend enforces it. VM pages show the launched type.
- Image lookup: AWS `data "aws_ami"` with `cf:os=<os>`, `cf:os-version=<imageVersion>`,
  `cf:managed=true`, `most_recent`; Azure uses the latest bake in the definition mapped to the
  selected release. Both ignore image changes after launch.
- `ssh_user` comes from the OS entry in `platform.json`: `ubuntu` for Ubuntu; `Administrator` for
  Windows on AWS, `cfadmin` on Azure (Azure forbids `Administrator`).
- Azure creates `azurerm_linux_virtual_machine` or `azurerm_windows_virtual_machine` by OS family (`count`;
  the Windows one launches with a random password that the access stage replaces).
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
  build.sh                     # build one linux/arm64 image, print its immutable local ID
  deploy.sh                    # ship a prebuilt image or build if omitted; verify the remote image ID
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
   - Day-2 runs (`POST /api/vms/runs`, from the VM page or `cf:vm:run`) read the VM entity with the
     initiating user's credentials, so a VM the user can't read fails before any run is created. Run pages, retry and skip are limited to the
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
| Platform | `aws`, `azure` (chosen on a Launch card; vSphere marked "Coming Soon") | Picks `terraform/platforms/<p>`; shown on the form with a "Change" link |
| Image | Ubuntu 26.04 (one card per offered OS/release) | Persists OS and `imageVersion`; selects the latest bake of the chosen release, not a newer release |
| Size | XS / S / M / L or an Advanced instance type | Mapped per platform |
| Software | checkboxes: git, htop, nginx, docker (from the software catalog, filtered by OS) | None picked = `software` stage skipped |
| AD join | checkbox (mock) | baseline + access always run |

How users get there: **Launch → Compute → Virtual Machines → Platform → Image**. The product page can
start at Platform. Images come from `GET /api/vm-sizes/platforms`; Continue prefills the template with
`compute.{platform,os,imageVersion}`. The stock template list at `/create` is not in the bar.

Steps:
1. The form has one step, `cf:vm:dispatch` (below). The mock ServiceNow request is no longer a
   Scaffolder step: it is the first stage of the provision run (6.1), executed by the IDP itself, so
   the whole pipeline (business steps and VM steps) is one run on one page with one failure model.
2. `cf:vm:dispatch` → the authenticated `vms` backend (`POST /api/vms/requests`), which reserves a
   demo quota slot and creates a provision run in the orchestrator (6). `request_id` is the Scaffolder
   task ID, so a task retry or recovery returns the same run. Identity and teams are server-derived
   (see 5.1a). Demo controls and cleanup semantics are defined in `CONTRACTS.md` §8.1.
3. The task then finishes and the IDP takes the browser to the run page (`/vms/runs/<id>`), the live
   view of the run: every stage with status, attempt, retry countdown, errors, the GitHub run and its steps, and
   Retry/Skip when a stage has failed for good. It does not depend on the VM's catalog entity, which
   appears a few seconds after `register` (see 5.3a). Scaffolder task recovery is on, so a backend
   restart mid-task resumes the task instead of failing it.
   The browser long-polls the task `/events` endpoint instead of SSE `/eventstream`, because
   TLS-inspecting corporate proxies (e.g. Netskope) buffer SSE and leave the page blank; see the IDP
   README for behavior and how to revert.
   Open tabs check for a newer deployed frontend and show a reload banner. Browser-side failures are
   reported to the backend log (`client-log` plugin), and Caddy keeps a JSON access log; see the IDP
   README "Logging and troubleshooting" and `./poc logs` / `./poc task`.
4. Output links: "Follow Provisioning" (the run page) and "Open VM in Catalog" (`resource:default/<name>`).

Backstage uses a **backend integration token** (a fine-grained PAT you create, scoped to
`compute-factory-platform`: Actions read/write, Contents read, **Commit statuses read**, which Backstage's
GitHub URL reader needs to resolve the commit SHA; without it, url Locations fail with 403). It
dispatches workflows and reads catalog files. Users therefore **don't need any access to the platform repo**.

### 5.3 Status and stages: run page and `vm-status` card

- The VM entity page header shows the VM's live state (latest run while active or failed, else the
  health check: Running, Stopped, Instance not found) and the day-2 actions (5.4). The body has
  **Connect** (copyable SSH command, Get SSH certificate), **Activity** (the active or failed run with
  its stage line and Retry/Skip, then the last 10 runs), **Configuration** (size, software, AD join,
  tags; Modify) and **Details** (health check, instance, IP, provenance, inventory source).
  The Virtual Machines product page and VM-only Resources results show each VM's latest run as a pipeline
  line. Home shows compact platform/state rows. All derive VM state from `/api/vms/summary`, falling
  back to `cf/status`.
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

- The stock template list (`/create`, no longer in the top bar; Launch replaces it) lists only `vm-request` (`sub-page:scaffolder/templates` `templateFilter` in the IDP
  `app-config.yaml`). Modify and SSH certificate are reached from the VM page (and the VM product page for the
  certificate); their direct template URLs still work, and the listing filter is not an authorization
  boundary.
- Day-2 actions, all on the VM page header. Each creates an orchestrator run (`POST /api/vms/runs`):
  - **Start / Stop / Reboot** (confirm dialog). On AWS stop releases the public IP and start gets a
    new one, which the stage records in inventory. On Azure stop deallocates the VM (no compute
    billing) and its static IP is kept. Reboot keeps the IP.
  - **Modify** opens the `vm-modify` form prefilled with the VM's current size, software and AD join.
    Only what changed is applied: a size change re-runs `provision` (Terraform updates the instance
    type in place: stop, change, start; a new IP on AWS, Azure keeps its static IP), software changes install checked and uninstall
    unchecked packages, AD join can be turned on or off.
  - **Apply OS updates** and **Re-apply configuration** (repair drift: baseline, software, AD join and
    access again) in the ⋯ menu, with a confirm dialog.
  - **Destroy** in the ⋯ menu; the dialog asks for the VM name. It supersedes any other active run.
  - **Retry / Skip** on a failed run, from the run page or the VM page (6.2).
  - Access: any member of the owning team connects with their personal SSH certificate (see 7), or
    with **Open terminal** in the VM page's Connect card: a browser terminal (xterm.js) whose SSH
    session runs on the IDP host with a 5-minute per-session cert for the VM's team. Keystrokes and
    output travel over short HTTP requests (long-polled output), not WebSockets, for the same proxy
    reason as the task page. Two sessions per user, 15-minute idle timeout, open/close logged.
    `CONTRACTS.md` §8.4.
  - Windows VMs add **Open Remote Desktop**: the IDP opens an SSH port forward to the VM's own RDP
    port (with the same kind of per-session cert; 3389 is never exposed) and a `guacd` sidecar
    (Apache Guacamole 1.6.0) connects through it with the VM's shared login. The browser draws the
    desktop with guacamole-common-js over a custom long-poll tunnel (Guacamole's own HTTP tunnel keeps
    responses open, which buffering proxies break; the WebSocket-only alternatives were rejected for
    the same reason: Teleport, Cloudflare, Devolutions/IronRDP, Warpgate, RD Web Client; AWS Fleet
    Manager and Azure Bastion work only in their consoles). One session per user, 30-minute idle
    timeout. **Show Password** reveals the shared login (logged) for native clients, which use
    `ssh -L 33389:localhost:3389 …` and a downloaded `.rdp` file. Everyone signs in as the same user,
    so a second person takes over the first one's desktop. `CONTRACTS.md` §8.5.
- Rules: a running run blocks every action except Destroy; a failed (incomplete) provision must be
  retried, skipped or destroyed first; any new action supersedes a failed day-2 run. Modify, Reboot,
  OS updates and Re-apply need a running VM; the backend refuses them (and a redundant start/stop) when
  the health check says otherwise.
- **Health check**: the IDP polls each cloud every 30 seconds (EC2 `DescribeInstances`/
  `DescribeInstanceStatus`; Azure VM power state, Resource Health and public IPs with the Reader
  principal; read-only, tag `cf:poc=true`), so a VM stopped or deleted outside the IDP shows as
  Stopped or "Instance not found". One cloud failing keeps the other's results. `CONTRACTS.md` §8.3.
- Team guard: see 5.1a.

## 6. Pipelines

Orchestration lives in the IDP; GitHub Actions executes. `CONTRACTS.md` §6 and §8.2 hold the exact
interfaces.

### 6.1 Runs and stages

The `vms` backend plugin (`compute-factory-idp/plugins/cf-backend/src/orchestrator.ts`) stores each
run and its stages in its database. Stage lists are code:

| Run | Stages (in order) |
|---|---|
| provision | `servicenow` (mock ServiceNow request, runs in the IDP) → `register` (validate + commit inventory) → `provision` (Terraform) → `baseline` → `software` (if picked) → `ad-join` (if picked, mock) → `access` → `finalize` (`cf/status: ready`) |
| modify | `configure` (persist the new desired compute and configuration) → `provision` (if size changed) → `software` (if changed) → `ad-join` (if changed) → `finalize` |
| reapply | `baseline` → `software` (if any) → `ad-join` (if enabled) → `access` → `finalize` |
| power | `start`, `stop` or `reboot` (one stage; no `finalize`, `cf/status` unchanged) |
| update | `os-update` (no `finalize`, `cf/status` unchanged) |
| destroy | `destroy` (guards, `cf/status: destroying`, `terraform destroy`, delete state and inventory) → `finalize` (failure path only) |

Each stage runs either inside the IDP (non-VM business steps such as the mock ServiceNow request;
future approvals or CMDB updates would go here) or as a `stage.yml` dispatch (everything that
touches cloud or VM). A 10-second worker runs or dispatches the next due stage, polls its GitHub run and job steps
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
request ID and refuses a different one. `provision` ignores image changes after creation, adopts
resources of the VM's `cf:request-id` that are missing from state instead of creating more (the
platform's `adopt.sh`: tagged EC2 instance/SG; the fixed-name resources of `cf-vm-<name>` on Azure),
and clears stale state locks. Fry stages are Ansible (`state: present`/`absent`, templated files), so
a re-run converges. Power stages succeed without changes when the instance is already in the target
state. `destroy` and `finalize` succeed without changes when the inventory
is already gone. All stages are serialized per VM by the workflow concurrency group `vm-<name>`.

### 6.4 How a stage reaches the VM (fry stages)

1. Generate an ephemeral SSH keypair on the runner.
2. Fetch the CA private key from Secrets Manager (`compute-factory/ssh-ca`, via OIDC), sign the
   ephemeral key (principal `cf-automation`, validity 1h), then delete the CA key file from the runner.
3. Wait for SSH, then `ansible-playbook -i <ip>, -u <ssh_user> ansible/fry/<stage>.yml`.

No per-VM keys and no key pairs in Terraform. The pipeline is just another CA client, like the users.
Windows uses the same path: OpenSSH Server is baked into the image with PowerShell as its shell, and
Ansible runs with `ansible_shell_type: powershell`. Readiness probes run `exit` (valid in every shell);
the reboot check compares the last boot time instead of `/proc/sys/kernel/random/boot_id`.

### 6.5 `bake.yml` (builder repo)

On push to `main` (paths `packer/**`, `ansible/**`, `images.yaml`): matrix over `images.yaml`, then AWS
OIDC (always: the CA public key is in SSM) and Azure OIDC for Azure rows, then `packer init` and
`packer build -only=<amazon-ebs|azure-arm>.ubuntu packer/ubuntu`. Packer's `ansible` provisioner
runs from the runner over SSH to the temporary build instance.

## 7. SSH CA (mock)

Model: **certs are per user, not per VM.** Every SSH client (users and the pipeline) holds its own keypair
and gets a short-lived cert from the CA. The cert's principals say which VMs it opens, and VMs never
store anyone's public key.

- The CA keypair (ed25519) is generated by Terraform in the image tier root `infra/images` (12.1). It
  lives exactly as long as the golden images that trust it: `./poc down --keep-images` keeps both, a
  plain `./poc down` destroys both, and the next `up` creates a new CA and bakes new images.
- **Public key**: SSM parameter `/compute-factory/ssh-ca-pub`. Bake reads it and puts it into every image,
  and tags every image `cf:ssh-ca-fingerprint` with its SHA256 fingerprint. `up` reuses an image only if
  that tag matches the current CA.
- **Private key**: stored in **one place**, the AWS Secrets Manager secret `compute-factory/ssh-ca`
  (ap-southeast-2). Two signers fetch it at signing time and keep it only in memory, never on disk:
  - the Backstage backend signs user certs. It reads the secret through the IDP EC2 instance role, which
    has `secretsmanager:GetSecretValue` on that secret ARN and on `compute-factory/vm/*` (Windows VM
    login passwords, shown to the owning team and used for browser RDP);
  - `stage.yml` (fry stages) signs the pipeline's throwaway certs. It reads the secret through the existing OIDC role.
- Rotation means updating the secret and re-baking with the new public key. Moving to Vault later only
  replaces the signing function in those two places.

| Who | Principals in cert | Validity | Issued by |
|---|---|---|---|
| Pipeline (fry) | `cf-automation` | 1h | `stage.yml`, ephemeral keypair per stage run |
| User | `team-<t>` for each team they're in, e.g. `team-a`, `team-b` | 12h | Backstage "Get SSH certificate" |
| Admin | adds `team-platform-admins` | 12h | same |

On each VM, `/etc/ssh/auth_principals/<login user>` (`ubuntu`) lists `cf-automation` and `team-platform-admins` (baked)
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
   - one of its principals is listed in `/etc/ssh/auth_principals/<login user>`;
   - it hasn't expired.

Check a cert with `ssh-keygen -L -f ~/.ssh/id_ed25519-cert.pub` (shows principals and expiry).

## 8. Adding Windows / vSphere later

| To add | Add |
|---|---|
| Windows OS | Done (Windows Server 2025): see below |
| Another image release | Publish the tagged AWS bake and the Azure bake under a separate Azure gallery definition. Then advertise the release in each supported platform's `os.<os>.versions` and map the Azure definition in `galleryImages`. |
| A platform (vSphere) | `terraform/platforms/<p>/` with the same contract plus `platform.json`, `sizes.json`, `adopt.sh`, `power.sh`; a Packer source and matrix row; its login step in `stage.yml`/`bake.yml`; the IDP's spec/health readers for that cloud (`vmSizes.ts`, `observed.ts`) and `PLATFORMS`; `./poc` sweeps; template enum. vSphere also needs a self-hosted runner that reaches vCenter |
| A product | Register its category, product ID, resource type, product page and launch template in the IDP; tag the template `product` and define its authorization policy before exposing inventory |
| New fry stage | `ansible/fry/<stage>.yml` + role, one entry in the orchestrator's stage list and `stage.yml`'s stage enum, one form checkbox |

Azure was added this way. It also exposed what the first version had hardcoded (the SSH user, the
cloud login, EC2-only validation and health checks), now read from `platform.json`.

Windows was **not** only new files. Besides `packer/windows/`, the Windows task files, `images.yaml`
rows, `os.windows` entries and the `windows-2025` gallery definition, shared code had to become OS-aware:
Ansible connection settings per family (`ansible/connection/`, no play-level `become`), the SSH probe
(`exit`) and reboot check, `os-update`, sizes per OS family (with a memory minimum), the AWS root volume
size, the default image version, Azure's Linux/Windows VM resources, the software catalog (JSON, read by
the forms through the IDP), the IDP's login-user check, and the Windows password and remote desktop
pieces (§5.4). Another OS family follows the same pattern; another release of an existing OS is the
"Another image release" row.

## 9. One-time manual steps (outside `./poc`)

1. **You**: create a fine-grained PAT for Backstage (GitHub doesn't allow creating it via the API) and put it
   in `compute-factory/.poc.env` (gitignored). It persists across up/down cycles.
   Set user passwords with `./poc set-password <user>`; they're kept in `compute-factory/.poc-users`.
2. Create the 4 private repos and push the code (done once, during the build).
3. Azure identities (done once, kept across up/down like the AWS OIDC role; need an interactive
   `az login` with Entra rights): the GitHub OIDC app `cf-github-actions` (Contributor, federated
   credentials for builder/platform `main`), the repo variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
   `AZURE_SUBSCRIPTION_ID`, `AZURE_LOCATION`, and the IDP's Reader principal `cf-idp-azure-reader`
   whose certificate is the gitignored `compute-factory/.poc-azure-reader.pem` (expires 2028-11-18).
   Laptop Terraform/`az` use the `cf-admin-local` service principal (`~/.zshrc` `ARM_*`, alias `azsp`).
   Resource providers Microsoft.Compute, Microsoft.Network and Microsoft.ResourceHealth are registered.

Everything else is done by `./poc up` (section 12).

End-to-end test after `up`, on each platform (AWS, Azure): get cert → Launch a VM → watch stages → SSH in
→ teammate SSH in → other team can't see it or SSH in → day-2 actions (stop/start/reboot, OS updates,
Modify) → destroy → `./poc down` → check that nothing is left on either cloud.

## 10. Rough cost (running 24/7)

IDP t4g.medium about $30/mo plus EIP/EBS. Each test VM (t3.small) costs about $0.03/hr. Windows
instances include the licence: roughly 1.5–2× the Linux price of the same type, and the Windows sizes
are larger (S = t3.large, about $0.13/hr on AWS). Browser RDP adds a `guacd` container on the IDP host
(1 GiB memory limit; per-session load to be measured on the first Windows VM; move the host to
t4g.large if 4 GiB proves too small). AMI snapshots
cost cents. An Azure test VM (2 vCPU v6) is roughly $0.10/hr plus a static IP and disk while it
exists; a deallocated one pays only those. Gallery storage costs cents. After `./poc down` the cost is
$0: no AWS or Azure resources remain. After `./poc down --keep-images` only the image tier remains:
the CA secret (about $0.40/month), AMI snapshots and gallery versions (cents).

## 11. Open questions

Resolved:
- Ubuntu: latest LTS, 26.04.
- Day-2 actions: start/stop/reboot, Modify (size, software install/uninstall, AD join), OS updates,
  re-apply configuration, destroy; SSH access via personal certs.
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
nothing is carried over between cycles except the code in Git, the two manual GitHub credentials (9),
Caddy's TLS storage and, only after `down --keep-images`, the image tier (SSH CA and golden images). `down` saves that to `.generated/caddy-data.tgz` on the laptop (gitignored) and `up`
restores it onto the new host, so a cycle doesn't request a new Let's Encrypt certificate (limit: 5 per week
for the same domain). Caddy renews it itself when it's within 30 days of expiry.

### 12.1 What lives where

```
compute-factory/                 # 4th repo: the "home" of the POC
  DESIGN.md
  poc                            # bash entrypoint: up [--images-only] | down [--keep-images] | status
  .poc.env                       # gitignored: Backstage PAT (from 9)
  .poc-users                     # gitignored: username:scrypt-hash lines (./poc set-password)
  infra/                         # shared Terraform root, LOCAL state (gitignored), applied only by ./poc
    main.tf                      #   default_tags { cf:poc = "true", cf:module = "infra" } on the AWS provider
    state-bucket.tf              #   S3 bucket for VM Terraform state; never force-empty
    idp.tf                       #   IDP EC2 (t4g.medium), EIP, SG, instance role (read the CA secret by name;
                                 #   read-only EC2 describe for sizes and the VM health check)
    azure.tf                     #   Azure reader secret compute-factory/azure-reader (from .poc-azure-reader.pem)
                                 #   + host-role read
    tokens.tf                    #   random_password: backend secret, PG password, BACKSTAGE_TOKEN
    outputs.tf
    images/                      # image tier root, its own LOCAL state; kept by down --keep-images
      main.tf                    #   AWS default_tags { cf:poc = "true", cf:module = "images" }; azurerm provider
      ssh-ca.tf                  #   tls_private_key (ed25519) -> Secrets Manager secret compute-factory/ssh-ca
                                 #   (recovery_window_in_days = 0) + SSM parameter /compute-factory/ssh-ca-pub
      gallery.tf                 #   Azure gallery cf-images/cfimages + image definition ubuntu (cf:module=images)
      outputs.tf                 #   includes ssh_ca_fingerprint (SHA256:..., the images' cf:ssh-ca-fingerprint)
  ../compute-factory-idp, ../compute-factory-builder, ../compute-factory-platform   # sibling clones
```

- The CA key is created by Terraform, so it exists only in local state and Secrets Manager. Golden
  images bake its public key, so the CA and the images form one tier with its own root: `infra/` refers
  to the CA secret only by name. A plain `down` destroys the tier and the next `up` makes a new CA, so
  old certs stop working. After `down --keep-images` the CA and images survive, and certificates issued
  before it stay valid until they expire (12h).
- The image tier's resources are tagged `cf:module=images` (AWS secret/parameter, Azure gallery group).
  AMIs, snapshots and gallery versions carry the Packer tags (`cf:managed=true`,
  `cf:ssh-ca-fingerprint`) and are created by the bake, not by Terraform.
- Before this split, `infra/` state held the CA and gallery. Lifecycle commands refuse such a state;
  move those resources with `terraform state mv -state=infra/terraform.tfstate
  -state-out=infra/images/terraform.tfstate <address> <address>` and apply `infra/images` (tag-only change).
- `recovery_window_in_days = 0` requests permanent deletion. AWS still processes it asynchronously;
  teardown verifies absence rather than assuming a successful delete request frees the name.
- The infra state stays local because a remote state bucket would have to survive `down`. It's
  disposable like everything else.

`up`, `down`, IDP deploy and registry maintenance share a per-user host lock across checkouts. An
inherited OS file descriptor keeps the lock held until surviving workers exit. Other hosts are not
covered: never run competing lifecycle or Terraform operations from a different machine. Lifecycle
commands use the default workspace and reject ambient `TF_CLI_ARGS*` / `TF_VAR_*` overrides.

### 12.2 `./poc up`

1. **Preflight**: check Docker/buildx, CLI tools, the AWS account, Azure subscription, certificate,
   SSH key, Cloudflare configuration, `.poc.env`, and repository AWS/Azure wiring. Refuse to apply
   if `infra/` state is unreadable/nonempty or platform `main` does not have an empty inventory.
   Gate and disable resource workflows; verify they are quiescent and the POC cloud/DNS scope is clean,
   except for a kept image tier (`cf:module=images` resources, the `cf-images` group and its contents,
   and AMIs/snapshots tagged `cf:managed=true`).
2. **Image tier**: apply `infra/images` (creates the CA and gallery if missing, otherwise no change) and
   require the SSM public key to match the CA fingerprint in its state.
3. **Image availability**: for every `images.yaml` row on builder `main`, find the
   image a new VM would launch (AWS: newest AMI with the row's tags; Azure: latest version of the
   gallery definition mapped in platform `platform.json`). If each is usable and tagged with the
   current CA fingerprint, reuse them without baking; warn, but still reuse, when an image was built
   from an older builder commit than `main`. Abort before applying shared infra if availability is
   unknown rather than treating a failed lookup as a missing image.
4. **Shared infra and bake**: apply `infra/` using the pinned AWS account. If images are missing or
   unusable, open only `bake.yml`. Request a run ID in the dispatch response
   and never retry an ambiguous dispatch. Poll that exact run. After `completed` + `success`, download
   the Packer manifest of every row of the run's commit and require each artifact to be the current
   usable image trusting the CA. Only after all rows pass, delete their superseded images (AMIs with
   snapshots, gallery versions); a failed prune only warns.
5. **IDP**: wire `TF_STATE_BUCKET`, `BACKSTAGE_URL` and `BACKSTAGE_TOKEN` in the platform repo.
   Wait for host initialization, apply the targeted Cloudflare DNS record, restore the TLS backup
   unless Caddy is already running, and deploy from a temporary clone of IDP `main`. Sibling checkouts
   are not rebased or used as bootstrap sources. Require HTTP 200 and authenticated catalog access/refresh
   before opening `stage.yml` and the platform gate.

`up` never clears inventory and has no force option. Failed bootstrap retains resources; check
`./poc status` and `.generated/logs/{images-apply,infra-apply,bake,idp}.log`. An exact dispatch ID is saved in
`.generated/bake-run-id`. `./poc watch-bake <run-id>` only monitors the run: it returns 0 for success,
1 for a completed non-success conclusion, and 2 if the outcome is unknown. The monitor polls every
30 seconds with a one-hour limit and stops after 10 consecutive failed status reads. It does not
verify artifacts, publish DNS, or deploy the IDP. `./poc up --resume <run-id>` completes an interrupted
bootstrap given a successful `bake.yml` dispatch on builder `main`. It checks that both resource
workflows are idle, the platform gate is still closed, inventory is empty, and Terraform points to
an owned running IDP, an owned empty bucket and the existing SSH CA. It verifies that run's manifests
and current images before continuing at step 5, and never reapplies infrastructure, dispatches or
prunes. It is not a repair command for a partial apply or a failed bake. `down` is intentional
teardown, not recovery for a failed verification read.

Image verification and workflow freeze/teardown GitHub reads retry up to four times (2, 4 and 8 seconds
between failures). A failed read cannot supply partial output; an unknown gate, run list or
inventory is not treated as an empty one. Dispatch is never retried automatically.

`./poc up --images-only` rebakes without touching anything else, whether the POC is up or down: it
applies `infra/images`, opens the builder gate and `bake.yml`, runs and verifies the bake as in step 4
(always baking) and prunes superseded images. On exit, including failure, it returns the builder gate
and `bake.yml` to their previous state; while the POC is down they are closed again. It does not check
cloud cleanliness, so use it on a POC that is up or was taken down with `down`/`down --keep-images`.

Routine `./poc deploy-idp [<image-id>]` skips infrastructure setup and image cleanup. Build with
`compute-factory-idp/deploy/build.sh`, smoke-test that local image ID, and pass the same ID to deploy
without rebuilding. The existing host runs the packaged Compose/Caddy config and is health-checked.
Run `./poc prune-idp-images` separately when the dedicated host needs disk space; the registry pauses
for garbage collection but the running application stays up.

### 12.3 `./poc down`

1. **Freeze**: set `CF_POC_MAINTENANCE=1` in both repos, manually disable the two resource workflows,
   and verify the freeze. Cancel outstanding runs from those workflows, including runs that become
   visible on a later poll. Require two empty polls and verify quiescence before sweeping. Save TLS
   from the tagged IDP when reachable, and stop it at EC2; the tagged sweep can still terminate it
   if stopping fails and the workflow freeze is proven.
2. **Protect VM recovery state**: inspect the POC-tagged S3 bucket and validate state and lock keys.
   A recent/unreadable lock blocks sweeping. After the freeze, a lock older than the platform's
   20-minute runner limit may be unlocked through Terraform by its recorded ID. From a temporary clone
   of platform `main`, populate a provider mirror serially, then run the workers in parallel (default
   5, `PARALLEL_DESTROYS` supports 1–20). Delete a VM's state object only after successful Terraform
   destroy. Keep the bucket, the image tier and inventory on a VM failure or if objects/versions
   remain; independent owned compute cleanup still runs once quiescence and lock safety are proven.
   Delete an empty bucket without force before shared Terraform destroy, including for old state
   that recorded `force_destroy = true`.
3. **Sweep owned resources**: terminate tagged AWS instances, sweep tagged detached disks, ENIs, EIPs,
   groups, key pairs and Windows VM password secrets (`compute-factory/vm/*`) in `ap-southeast-2` only. The POC does not discover or touch other AWS regions;
   check those separately if a manual run used one. When VM state is resolved and `--keep-images` was
   not given, deregister tagged AMIs including disabled ones before deleting independently tagged
   snapshots. Delete tagged Azure VM and build groups; then, unless images are kept, remove gallery
   versions only from the verified POC-owned (`cf:module=images`) gallery resource group.
   `NetworkWatcherRG` is unowned and left alone. AWS named fallbacks require ownership tags, and
   untagged Packer-name candidates are reported, never deleted.
4. **Shared infra, image tier and DNS**: remove the targeted Cloudflare record. Destroy `infra/` only
   when the VM bucket can safely be removed. Destroy `infra/images` (CA and gallery) only when images
   are not kept and the Azure VM/build groups and gallery versions are gone; it uses the Azure
   certificate defaults. Independent AWS/Azure sweeps still apply when Terraform cannot complete; the
   CA fallback runs only when the image tier is being removed. Do not force-empty the state bucket.
   Retry Terraform state reconciliation of both roots after fallback cleanup when possible.
5. **Verify before GitHub cleanup**: require empty local `infra/` state (and `infra/images` state unless
   images are kept). Run Sydney AWS, Azure POC-tag and DNS checks with workflow quiescence checked
   before and after. The first complete successful pass is enough; retry failures up to six attempts
   total, with starts at least 30 seconds apart. Check both service inventories and the AWS Resource
   Groups Tagging API. Batch exact ID lookups per supported EC2 resource type and ignore stale tag
   entries only when EC2 confirms the resource is absent or the instance is terminated. Live matches,
   unsupported entries and failed lookups still block `CLEAN`. With `--keep-images` the checks allow
   the image tier (see 12.2 step 1).
6. **GitHub cleanup**: verify deletion of platform wiring and reset `vms/` on platform `main` with a
   normal non-force push. Verify the published inventory and workflow freeze before printing `CLEAN`,
   or `CLEAN (images retained)` with the kept CA fingerprint and images for `--keep-images`.

`down --keep-images` keeps the image tier on request. Retention forced by a failed VM teardown is
different: it keeps the bucket and image tier for recovery and still ends `NOT CLEAN`. A later plain
`down` removes a kept image tier, including when nothing else exists.

`NOT CLEAN` means teardown could not prove completion. Check `.generated/logs/` and the reported
resource IDs; fix the blocker and rerun. A retained bucket/image or a resource outside automated
ownership rules may still incur charges. Never clear state just to obtain `CLEAN`.

### 12.4 `./poc status`

A read-only tag sweep plus `gh run list`. It shows what currently exists (IDP up?, the SSH CA
fingerprint, AMIs and gallery versions with their CA fingerprint and git SHA, VMs, running pipelines),
so you can check before or after `up`/`down`.

### 12.5 Rules this puts on the rest of the design

- Every Terraform AWS provider (`infra`, `infra/images` and `terraform/platforms/aws`) sets `default_tags { cf:poc = "true" }`.
  Packer sets `tags`, `run_tags`, `run_volume_tags` and `snapshot_tags` with `cf:poc = "true"`.
  Azure has no provider default tags: every Azure resource and resource group sets the `cf:*` tags
  itself (Packer `azure_tags` tags its temporary build group).
- No resource may be created outside Terraform or Packer, apart from Packer's temporary key pairs and
  security groups. Teardown detects any remaining named Packer temporary resources and fails closed
  rather than deleting by an unowned name prefix.
- `down` removes only Terraform-tracked or POC-owned AWS/Azure resources, the targeted Cloudflare
  `idp` record, and the POC's GitHub wiring and `vms/`. Unrelated Azure groups may remain.
  The Azure identities and `AZURE_*` variables (9) persist like the AWS OIDC role.
