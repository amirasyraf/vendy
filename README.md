# Compute Factory (POC)

An IDP (Backstage) for self-service products across platforms. Home groups visible resources by product.
The first product, **Compute → Virtual Machines**, runs on **AWS and Azure** (Ubuntu 26.04 and Windows
Server 2025, the latter with browser Remote Desktop over SSH) with
Packer, Terraform and Ansible on GitHub Actions. Users choose a platform and an image release before
configuring the VM. The dedicated **Virtual Machines** page shows live state and pipelines across clouds.
AWS is the shared base (SSH CA, Terraform state, IDP host); each platform is a
`terraform/platforms/<p>/` directory in the platform repo.

Each VM's `catalog-info.yaml` is its single desired-configuration source: structured `spec.compute`,
`spec.configuration`, optional `spec.billing` and `spec.cloudTags`. Workflows read that file for
Terraform/Ansible; annotations hold observed status and outputs. There is no separate `request.yaml`
and no automatic reconciliation on Git edits. See `CONTRACTS.md` §7 for the validated schema.

- [DESIGN.md](DESIGN.md): architecture and decisions
- [CONTRACTS.md](CONTRACTS.md): exact cross-repo names and interfaces
- [AGENTS.md](AGENTS.md): project rule that documentation stays synchronized with behavior

| Repo | Purpose |
|---|---|
| `compute-factory` (this) | design, `./poc` lifecycle, shared AWS infra (`infra/`), SSH CA + image gallery (`infra/images/`) |
| `compute-factory-idp` | Backstage app + deployment |
| `compute-factory-builder` | bake: golden images (Packer + Ansible) |
| `compute-factory-platform` | provision + fry: Terraform, Ansible, GHA workflows, VM inventory, templates |

## Lifecycle

Sibling clones live in the same parent directory as this repo (`./poc up` clones any that are missing
without changing existing checkouts). Bootstrap and teardown use temporary clones of canonical `main`.

```bash
cp poc.env.example .poc.env   # once: Backstage PAT
./poc set-password amirasyraf  # once per user: IDP login (hash kept in .poc-users)
./poc up                      # image tier, shared infra, bake (or reuse matching images), then IDP deploy
./poc up --resume <run-id>     # finish a gated bootstrap using an existing successful bake; no rebake or pruning
./poc up --images-only        # SSH CA/gallery if missing, bake + verify every images.yaml row, prune old images
./poc status                  # what exists right now
./poc watch-bake <run-id>      # monitor an existing bake; never dispatches another
./poc deploy-idp              # build and deploy Backstage on the existing host
./poc deploy-idp "$image"     # ship an already-built image by its full sha256 image ID
./poc prune-idp-images         # explicit maintenance: collect old registry blobs and dangling IDP images
./poc logs [backstage|caddy|postgres|all] [since]   # IDP container logs (default backstage, 30m)
./poc task <task-id>          # Scaffolder task status + stored events from Postgres
./poc run <run-id>            # VM orchestrator run + stage state (attempts, GitHub run IDs, errors)
./poc down                    # freeze workflows, destroy VMs and infra, sweep owned orphans, verify CLEAN
./poc down --keep-images      # the same, but keep the SSH CA and golden images for the next up
```

For an application change, [build and smoke-check one local image](../compute-factory-idp/README.md#checks)
and pass its image ID to `./poc deploy-idp`. Routine releases use the existing DNS and Caddy storage,
do not run image cleanup, and report stage timings. Start with the hot-reload dev server for layout
iterations.

Bake monitoring polls GitHub every 30 seconds and retries status-read errors. A temporary API timeout
does not mean the workflow failed. Monitoring stops after 10 consecutive read errors or one hour,
reporting an **unknown outcome**, not a failed bake. Only a completed run with conclusion `success`
counts as a successful bake. `watch-bake` exits 0 for success, 1 for a completed non-success conclusion,
or 2 when monitoring cannot determine the outcome.

If `up` stops, it leaves resources in place. Check `./poc status` and `.generated/logs/`.
`./poc watch-bake <run-id>` resumes monitoring only. The dispatch ID is saved in `.generated/bake-run-id`.
If dispatch did not return an ID, inspect builder Actions; the script does not replay an ambiguous dispatch.
Once the exact bake has succeeded, run `./poc up --resume <run-id>` to verify its artifacts and finish
bootstrap on the existing host. Resume requires the platform gate to be closed, both workflows idle,
and no VM inventory or state. It does not reapply shared/image infrastructure, dispatch a bake, or prune images.
It cannot repair a partial Terraform apply or recover a failed bake. Plain `up` still requires empty
shared state. Neither form resets inventory; `down` is only for intentional teardown of the POC
**and all requested VMs**. The VM workflow opens only after successful IDP readiness and catalog checks.

GitHub reads used in image verification and workflow freeze/teardown checks get up to four attempts.
An unreadable image lookup does not trigger a bake, and an unreadable inventory does not trigger an
inventory reset. If an intentional `down` stops without reporting `CLEAN`, fix the reported blocker and
rerun it with the same retention option.

Golden images bake the SSH CA public key, so the CA and images are one tier with its own Terraform root,
`infra/images` (DESIGN.md §12.1). Every image is tagged `cf:ssh-ca-fingerprint`. `up` reuses the images
when each `images.yaml` row's current image trusts the current CA and bakes otherwise; if a reused image
was built from an older builder commit than `main` it only warns, so run `./poc up --images-only` after
changing the image recipe. `--images-only` works whether the POC is up or down and returns the builder
gate to its previous state afterwards. A new bake prunes superseded images only after every row is verified.

After a VM request, the browser goes to the run page: every stage of the run (ServiceNow mock,
registration, Terraform provisioning, every fry stage, finalization) with retries, errors and GitHub
runs. The VM page shows the same live, team-scoped view.

VM requests first demonstrate a real REST POST to the IDP's authenticated mock ServiceNow endpoint
(`/mock-rest/servicenow`), returning HTTP 201 and a fake request number before GHA dispatch. It creates
no real ticket and performs no approval. See `CONTRACTS.md` §8 and the IDP README for details.

Requirements: `terraform`, `gh` (authenticated), `aws` (profile `amirasyraf`), `az` (logged in to
subscription `74b39a79-…`, e.g. `azsp`), plus the `cf-admin-local` `.pfx` certificate at
`~/.config/azure/cf-admin-local.pfx` (the POC script supplies its fixed `ARM_*` defaults and accepts
explicit environment overrides). The gitignored Azure reader certificate `.poc-azure-reader.pem`,
`docker` (buildx, for the arm64 IDP image), `jq`, `python3` with PyYAML, `ssh`/`ssh-keygen` with
`~/.ssh/id_ed25519`, and `dig`
are also needed. A per-user host lock covers lifecycle and IDP deploy/maintenance commands across
checkouts, including their active workers. Do not run POC mutations from another host or directly
against its Terraform state. Lifecycle commands use the default Terraform workspace and reject
`TF_CLI_ARGS*`/`TF_VAR_*` overrides.
The Azure identities and `AZURE_*` repo variables are one-time setup that survives `down` (DESIGN.md §9).

`down` is safe to rerun after a partial `up` or `down`. It gates and disables both resource workflows,
cancels runs until they are quiescent, then stops the owned IDP. A failed VM destroy retains the state
bucket and the image tier for recovery (still `NOT CLEAN`); independent owned compute cleanup still runs when lock safety
and quiescence can be proven. The retained state or images can still incur charges. Check the named
logs and leftover checks, fix the reported blocker, and rerun; do not empty the bucket manually.
Azure gallery deletion can take several minutes. Untagged/shared resources such as `NetworkWatcherRG`
are not removed, and possible legacy AWS Packer leftovers are reported rather than deleted by name.

AWS regional cleanup and verification are limited to Sydney (`ap-southeast-2`). Check any other
region separately if a manual run used it. `CLEAN` requires a successful AWS, Azure and DNS check,
empty local Terraform state, cleared GitHub wiring, an empty `vms/` inventory, and workflows verified
disabled and idle. Stale AWS tag entries for EC2 resources are ignored only when EC2 confirms the
resource is gone or its instance is terminated. With
`--keep-images` the same checks allow exactly the image tier (CA secret and parameter, the `cf-images`
gallery and AMIs/snapshots tagged `cf:managed=true`) and report `CLEAN (images retained)`; it costs
cents a month plus about $0.40 for the CA secret. A later plain `down` removes it. The manual Azure
identities, repository credentials and Caddy TLS backup remain by design.

The `idp.amirasyraf.dev` DNS record lives in `~/projects/cloudflare/amirasyraf.dev/idp.tf` and is only
applied with `-target` by `./poc`.
