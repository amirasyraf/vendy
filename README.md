# Compute Factory (POC)

An IDP (Backstage) and a compute platform (Packer + Terraform + Ansible on GitHub Actions) for self-service VMs.

Each VM's `catalog-info.yaml` is its single desired-configuration source: structured `spec.compute`,
`spec.configuration`, optional `spec.billing` and `spec.cloudTags`. Workflows read that file for
Terraform/Ansible; annotations hold observed status and outputs. There is no separate `request.yaml`
and no automatic reconciliation on Git edits. See `CONTRACTS.md` §7 for the validated schema.

- [DESIGN.md](DESIGN.md): architecture and decisions
- [CONTRACTS.md](CONTRACTS.md): exact cross-repo names and interfaces
- [AGENTS.md](AGENTS.md): project rule that documentation stays synchronized with behavior

| Repo | Purpose |
|---|---|
| `compute-factory` (this) | design, `./poc` lifecycle, shared AWS infra (`infra/`) |
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
./poc deploy-idp              # rebuild + redeploy Backstage on the existing host
./poc logs [backstage|caddy|postgres|all] [since]   # IDP container logs (default backstage, 30m)
./poc task <task-id>          # Scaffolder task status + stored events from Postgres
./poc run <run-id>            # VM orchestrator run + stage state (attempts, GitHub run IDs, errors)
./poc down                    # ~5-10 min: cancels runs, destroys all VMs, sweeps, destroys infra, verifies CLEAN
```

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

After a VM request, the Scaffolder task streams the complete workflow process graph. The VM's catalog page
shows the same live, team-scoped view: validation, inventory registration, Terraform provisioning, every fry
job, finalization and failure handling, plus request details, refresh time and the full Actions run link.

VM requests first demonstrate a real REST POST to the IDP's authenticated mock ServiceNow endpoint
(`/mock-rest/servicenow`), returning HTTP 201 and a fake request number before GHA dispatch. It creates
no real ticket and performs no approval. See `CONTRACTS.md` §8 and the IDP README for details.

Requirements: `terraform`, `gh` (authenticated), `aws` (profile `amirasyraf`), `docker` (buildx, for the arm64 IDP image), `jq`, `ssh` with `~/.ssh/id_ed25519`.

`down` is safe to run repeatedly and after a failed `up`. Everything in AWS is tagged `cf:poc=true`;
`down` ends by checking that nothing with that tag remains.

The `idp.amirasyraf.dev` DNS record lives in `~/projects/cloudflare/amirasyraf.dev/idp.tf` and is only
applied with `-target` by `./poc`.
