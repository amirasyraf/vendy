# Compute Factory (POC)

An IDP (Backstage) for self-service products across platforms. Home groups visible resources by product.
The first product, **Compute → Virtual Machines**, runs on **AWS and Azure** (Ubuntu 26.04) with
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
| `compute-factory` (this) | design, `./poc` lifecycle, shared AWS + Azure infra (`infra/`) |
| `compute-factory-idp` | Backstage app + deployment |
| `compute-factory-builder` | bake: golden images (Packer + Ansible) |
| `compute-factory-platform` | provision + fry: Terraform, Ansible, GHA workflows, VM inventory, templates |

## Lifecycle

Sibling clones live in the same parent directory as this repo (`./poc up` clones any that are missing).

```bash
cp poc.env.example .poc.env   # once: Backstage PAT
./poc set-password amirasyraf  # once per user: IDP login (hash kept in .poc-users)
./poc up                      # ~15-20 min: infra, GitHub wiring, bake, IDP deploy
./poc status                  # what exists right now
./poc watch-bake <run-id>      # monitor an existing bake; never dispatches another
./poc deploy-idp              # build and deploy Backstage on the existing host
./poc deploy-idp "$image"     # ship an already-built image by its full sha256 image ID
./poc prune-idp-images         # explicit maintenance: collect old registry blobs and dangling IDP images
./poc logs [backstage|caddy|postgres|all] [since]   # IDP container logs (default backstage, 30m)
./poc task <task-id>          # Scaffolder task status + stored events from Postgres
./poc run <run-id>            # VM orchestrator run + stage state (attempts, GitHub run IDs, errors)
./poc down                    # ~5-10 min: cancels runs, destroys all VMs, sweeps, destroys infra, verifies CLEAN
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

If `up` cannot finish, it leaves resources and workflows in place. Check `./poc status` and
`.generated/logs/{bake,idp}.log`; resume monitoring with `./poc watch-bake <run-id>` using the logged
run ID. If dispatch/run discovery could not be confirmed, find the run in the builder repo's Actions
page first; dispatch is never automatically retried. `watch-bake` only checks the bake, not IDP health
or AMI existence. Do not rerun `up` just to reconnect the watcher: it is not a resume command and resets
VM inventory. Use `./poc down` only for intentional teardown of the POC **and all requested VMs**.

After a VM request, the browser goes to the run page: every stage of the run (ServiceNow mock,
registration, Terraform provisioning, every fry stage, finalization) with retries, errors and GitHub
runs. The VM page shows the same live, team-scoped view.

VM requests first demonstrate a real REST POST to the IDP's authenticated mock ServiceNow endpoint
(`/mock-rest/servicenow`), returning HTTP 201 and a fake request number before GHA dispatch. It creates
no real ticket and performs no approval. See `CONTRACTS.md` §8 and the IDP README for details.

Requirements: `terraform`, `gh` (authenticated), `aws` (profile `amirasyraf`), `az` (logged in to
subscription `74b39a79-…`, e.g. `azsp`), the `cf-admin-local` service principal's `ARM_*` variables
(`~/.zshrc`; Terraform azurerm needs the `.pfx` certificate), the gitignored Azure reader certificate
`.poc-azure-reader.pem`, `docker` (buildx, for the arm64 IDP image), `jq`, `ssh` with `~/.ssh/id_ed25519`.
The Azure identities and `AZURE_*` repo variables are one-time setup that survives `down` (DESIGN.md §9).

`down` is safe to run repeatedly and after a failed `up`. Everything in AWS and Azure is tagged
`cf:poc=true`; `down` ends by checking that nothing with that tag remains on either cloud. It also
removes `NetworkWatcherRG`, which Azure creates untagged with the first VNet, while it holds only those
automatic watchers. `up` checks the one-time Azure setup first and stops if it is missing.

The `idp.amirasyraf.dev` DNS record lives in `~/projects/cloudflare/amirasyraf.dev/idp.tf` and is only
applied with `-target` by `./poc`.
