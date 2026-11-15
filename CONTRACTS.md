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

- Groups (`kind: Group`, `spec.type: team`): `alpha`, `bravo`, `platform-admins`.
- User `amirasyraf` (GitHub username) is in `memberOf: [alpha]`. More users are added later by the orchestrator.
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

All `workflow_dispatch` inputs are strings. Every dispatchable workflow sets
`run-name: "<workflow> <name> by <requested_by>"`.

### 6.1 `provision.yml` inputs

| Input | Example | Notes |
|---|---|---|
| `name` | `demo-1` | `^[a-z][a-z0-9-]{2,20}$` |
| `team` | `alpha` | must be in `requester_teams` |
| `platform` | `aws` | only `aws` supported now |
| `os` | `ubuntu` | only `ubuntu` supported now |
| `size` | `S` | `S`/`M`/`L` → `t3.small`/`t3.medium`/`t3.large` |
| `software` | `git,nginx` | comma-separated, may be empty |
| `ad_join` | `true` | `"true"`/`"false"` |
| `requested_by` | `amirasyraf` | Backstage user name |
| `requester_teams` | `alpha,bravo` | comma-separated group names, server-side from Backstage |

### 6.2 Other workflow inputs

- `destroy.yml`: `name`, `requested_by`, `requester_teams`.
- `fry.yml`: `name`, `stage` (`baseline`|`software`|`ad-join`|`access`), `software` (comma list, used when stage=software), `requested_by`, `requester_teams`.
- `_fry-stage.yml` (`workflow_call`, internal): `name`, `stage`, `host`, `ssh_user`, `os`, `vars_json`.
  The caller grants `id-token: write` and `contents: write`.

Team guard: `provision` requires `team ∈ requester_teams`. `destroy` and `fry` require the VM's `team`
(from `vms/<name>/request.yaml`) `∈ requester_teams`. A `platform-admins` member passes all guards.

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

`request.yaml`:
```yaml
name: demo-1
team: alpha
platform: aws
os: ubuntu
size: S
software: [git, nginx]
ad_join: true
requested_by: amirasyraf
requested_at: "2026-11-15T12:00:00Z"
run_id: "1234567890"
```

`catalog-info.yaml`:
```yaml
apiVersion: backstage.io/v1alpha1
kind: Resource
metadata:
  name: demo-1
  title: demo-1
  description: ubuntu VM on aws (S) for team alpha
  annotations:
    cf/status: provisioning        # provisioning | frying | ready | failed | destroying
    cf/stages: '{"provision":"running","baseline":"pending","software":"pending","ad-join":"pending","access":"pending"}'
    cf/platform: aws
    cf/os: ubuntu
    cf/size: S
    cf/team: alpha
    cf/software: git,nginx
    cf/public-ip: ""               # filled after provision
    cf/ssh-user: ubuntu
    cf/instance-id: ""
    cf/requested-by: amirasyraf
    cf/run-id: "1234567890"
    cf/run-url: https://github.com/amirasyraf/compute-factory-platform/actions/runs/1234567890
spec:
  type: vm
  owner: group:default/alpha
  lifecycle: poc
```

- Stage values: `pending | running | success | failed | skipped`.
- Stage keys are always these five, in this order: `provision`, `baseline`, `software`, `ad-join`, `access`.
- `vms/.gitkeep` always exists.

## 8. Backstage ↔ platform integration

- `BACKSTAGE_URL` = `https://idp.amirasyraf.dev` (platform repo variable).
- `BACKSTAGE_TOKEN` (platform repo secret) is a Backstage static external-access token restricted to the catalog plugin.
- After each inventory commit, the workflow calls (best-effort, never fails the job):
  `POST $BACKSTAGE_URL/api/catalog/refresh` with header `Authorization: Bearer $BACKSTAGE_TOKEN` and JSON
  body `{"entityRef":"resource:default/<name>"}`. On the first commit it also refreshes
  `location:default/cf-vms`.
- Catalog locations (idp `app-config.production.yaml`, or `catalog/locations.yaml`):
  - Location entity **named `cf-vms`**, type `url`, target
    `https://github.com/amirasyraf/compute-factory-platform/blob/main/vms/*/catalog-info.yaml`;
  - templates: `https://github.com/amirasyraf/compute-factory-platform/blob/main/catalog/templates/*/template.yaml`;
  - org and the ssh-cert template come from the idp repo itself.
- `catalog.orphanStrategy: delete`. Catalog processing interval should be short (POC).
- Template names (`metadata.name`): `vm-request`, `vm-destroy`, `vm-refry` (platform repo, Agent B) and
  `ssh-cert` (idp repo, Agent C).
- Templates dispatch with `github:actions:dispatch`:
  - `repoUrl: github.com?owner=amirasyraf&repo=compute-factory-platform`, `branchOrTagName: main`;
  - pass `requested_by: ${{ user.entity.metadata.name }}`;
  - pass `requester_teams: ${{ user.entity.spec.memberOf | join(',') }}`. `memberOf` may contain refs like
    `group:default/alpha`, so the workflow normalises by stripping everything up to the last `/` or `:`.
  - VM pickers use `EntityPicker` with `catalogFilter: { kind: Resource, spec.type: vm }`.
  - The team picker is the stock `MyGroupsPicker` (Groups with `relations.hasMember` = the current user). It
    returns an entity ref, which the workflow normalises to the bare name.
  - The browser link to the run is the dispatch output `workflowRunHtmlUrl`. `workflowRunUrl` is the API URL.
- Custom scaffolder action (idp repo, Agent C):
  - `cf:ssh:sign`, input `{ publicKey: string }`, output `{ certificate, principals, validUntil }`;
  - reads the CA key from Secrets Manager `compute-factory/ssh-ca` (region from `AWS_REGION`) using
    default AWS credentials, which on EC2 come from the instance role through IMDS (hop limit 2 is set by infra).

## 9. IDP runtime (orchestrator infra ↔ Agent C deploy)

- EC2: Ubuntu 26.04 **arm64** (t4g.medium). Cloud-init installs Docker Engine + compose plugin, puts
  `ubuntu` in the `docker` group, and creates `/opt/idp` owned by `ubuntu`. IMDSv2 has hop limit 2.
- SSH: `ssh -i ~/.ssh/id_ed25519 ubuntu@<ip>` (EC2 key pair from that key).
- `compute-factory-idp/deploy/deploy.sh <host> <env-file>`:
  - builds image `compute-factory-idp:latest` for `linux/arm64` locally;
  - `docker save | ssh docker load`;
  - copies `docker-compose.yml`, `Caddyfile` and `<env-file>` (as `.env`) to `/opt/idp`;
  - runs `docker compose up -d`.
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
