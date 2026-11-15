# Compute Factory (POC)

An IDP (Backstage) and a compute platform (Packer + Terraform + Ansible on GitHub Actions) for self-service VMs.

- [DESIGN.md](DESIGN.md): architecture and decisions
- [CONTRACTS.md](CONTRACTS.md): exact cross-repo names and interfaces

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
./poc deploy-idp              # rebuild + redeploy Backstage on the existing host
./poc down                    # ~5-10 min: cancels runs, destroys all VMs, sweeps, destroys infra, verifies CLEAN
```

Requirements: `terraform`, `gh` (authenticated), `aws` (profile `amirasyraf`), `docker` (buildx, for the arm64 IDP image), `jq`, `ssh` with `~/.ssh/id_ed25519`.

`down` is safe to run repeatedly and after a failed `up`. Everything in AWS is tagged `cf:poc=true`;
`down` ends by checking that nothing with that tag remains.

The `idp.amirasyraf.dev` DNS record lives in `~/projects/cloudflare/amirasyraf.dev/idp.tf` and is only
applied with `-target` by `./poc`.
