# Vendy

Vendy is a self-service developer portal built on Backstage. Its first offering is on-demand
VMs: choose Ubuntu 26.04 or Windows Server 2025 on AWS or Azure, follow the build, then connect
over SSH. Windows also supports Remote Desktop through SSH.

Open [idp.amirasyraf.dev](https://idp.amirasyraf.dev) when the POC is up. Sign in with your Vendy
account and choose **Launch → Compute → Virtual Machines**. Use the run page to see what the
workflow is doing. Use the VM page to connect, change its configuration, or destroy it.

The second offering is **Launch → Compute → Containers**: give a public image, port, size, cloud and
CPU architecture, and get `https://<name>.apps.idp.amirasyraf.dev`. Worker nodes start on demand on
AWS or Azure, so the first container on an idle pool takes a few minutes. See
[DESIGN.md §13](DESIGN.md#13-containers).

## Repositories

| Repo | Purpose |
|---|---|
| `vendy` (this repo) | POC lifecycle, shared infrastructure, SSH CA, and Azure image gallery |
| [`vendy-idp`](https://github.com/amirasyraf/vendy-idp) | The Backstage portal and its deployment scripts |
| [`vendy-builder`](https://github.com/amirasyraf/vendy-builder) | Golden images built with Packer and Ansible |
| [`vendy-platform`](https://github.com/amirasyraf/vendy-platform) | VM and container templates, provisioning and configuration, and Git inventory |

The platform repo is public, including its VM inventory and commit history. Portal team permissions
do not hide those files from GitHub readers. Use dummy request metadata and never enter secrets in
requests, billing fields, or cloud tags.

## Lifecycle

Read [setup and credentials](OPERATIONS.md#setup-and-credentials) before your first run. Clone
`vendy-idp` beside this repo before setting the first password. `./poc up` can clone the other
missing repos. It doesn't overwrite local checkouts and builds the portal from GitHub's `main`.
Full `up` generates the portal's Azure Reader credential; there is no local Reader PEM to supply.

```bash
test -e .poc.env || cp poc.env.example .poc.env
```

Fill in `GITHUB_TOKEN`, then use the command that matches what you want to do:

| Command | Use it to… |
|---|---|
| `./poc set-password <username>` | Set a local POC login; the next deploy loads its hash. |
| `./poc up` | Bring up the POC from empty shared state. |
| `./poc up --gha` | Bring up the same POC, building and smoke-testing the IDP image on GitHub instead of the laptop. |
| `./poc up --gha --resume [<bake-id>] [--idp-run <run-id>]` | Finish a gated Actions bootstrap. Uses the original build unless given an exact replacement IDP run. |
| `./poc status` | See resources, readiness, and recent pipeline runs. |
| `./poc watch-bake <run-id>` | Check an existing bake without dispatching another. |
| `./poc up --resume <run-id>` | Finish a gated bootstrap using a dispatched bake; the IDP deploys while that bake finishes. |
| `./poc up --images-only` | Rebuild and verify golden images, then prune superseded images. |
| `./poc deploy-idp [<image-id>]` | Update the app on the existing host. |
| `./poc deploy-idp --gha [<run-id>]` | Update the existing host from a tested Actions build; no local Docker or image transfer. |
| `./poc prune-idp-images` | Clean up unused IDP images and registry blobs. |
| `./poc logs [service] [since]` | Read service logs; defaults to `backstage` and `30m`. |
| `./poc task <task-uuid>` / `./poc run <run-id>` | Inspect a Scaffolder task or a VM run. |
| `./poc down [--keep-images]` | Tear down the POC and all its VMs. The optional flag keeps the CA and images. |

For a portal update from pushed `main`, use `./poc deploy-idp --gha`. For unpushed work, [build and
check one local image](https://github.com/amirasyraf/vendy-idp#checks) and pass its image ID to
`./poc deploy-idp`. Both use the existing host and leave DNS alone.

**`./poc down` destroys the POC and its VMs.** Don't use it to recover from a failed bake or deploy.
Read [the runbook](OPERATIONS.md#diagnose-before-retrying) if bootstrap stalls or teardown reports
`NOT CLEAN`.

## More detail

- [Operations](OPERATIONS.md): setup, credentials, recovery, and verification
- [Design](DESIGN.md): how the portal, workflows, and cloud resources fit together
- [Contracts](CONTRACTS.md): shared schemas, exact identifiers, and workflow behavior
- [Agent instructions](AGENTS.md): rules for automated contributors
