# Vendy POC operations

Use this runbook to choose an action and find evidence. [README.md](README.md) lists `poc` commands,
[DESIGN.md §12](DESIGN.md#12-lifecycle-poc-up--poc-down) explains their order, and
[CONTRACTS.md](CONTRACTS.md) records the interfaces. Run lifecycle commands from the root checkout
that owns the local Terraform state. Don't run competing lifecycle or Terraform mutations from
another checkout or host.

## Setup and credentials

Bootstrap needs Terraform, Git, an authenticated `gh`, the AWS and Azure CLIs, `uv` (`./poc` provisions
its own Python 3.10+ and PyYAML), `curl`, and OpenSSH. Local-build mode additionally needs Docker
with buildx and Compose and `scp`. `--gha` needs no local Docker.
The host key pair is `~/.ssh/id_ed25519` and `.pub`. The repo pins Node via `mise` in `vendy-idp`.

1. Put `vendy` and `vendy-idp` side by side; `./poc set-password` uses the local IDP hash helper
   through `mise` and Node 24. Bootstrap can clone any remaining missing sibling checkouts over SSH.
   Bootstrap builds the published IDP `main`, not uncommitted changes in the sibling checkout.
2. Check the operator's GitHub CLI login, Git SSH access, AWS account/profile, and selected Azure
   subscription. See [CONTRACTS.md §1](CONTRACTS.md#1-aws-constants) for the pinned account, regions,
   and workload identities.
   The builder and platform repositories need the `AWS_*` and `AZURE_*` variables in the contract.
   Entra federation needs an exact subject match, including the repository **name** and IDs.
3. Set `GITHUB_TOKEN` in gitignored `.poc.env` using `poc.env.example`. Create a fine-grained PAT scoped
   to `vendy-platform` with Actions read/write, Contents read, Commit statuses read, and Metadata
   read. GitHub CLI operator access is separate. Don't pass the PAT on a command line or print it.
4. Create the portal user in `vendy-idp/catalog/org.yaml`, grant only the needed team membership,
   and run `./poc set-password <username>`. Bootstrap or a routine IDP deploy loads the hashes;
   editing local password files does not update the running portal. Treat `.poc-users` and the
   plaintext `.poc-passwords` as private, gitignored files. The old GitHub OAuth app and the legacy
   `AUTH_GITHUB_CLIENT_ID` / `AUTH_GITHUB_CLIENT_SECRET` entries are not required.
5. Set `ARM_CLIENT_ID` and `ARM_CLIENT_CERTIFICATE_PATH` to your operator service principal's
   application ID and PKCS#12 certificate; set `ARM_CLIENT_CERTIFICATE_PASSWORD` if needed.
   Full `up`/`down` also use that operator certificate for Microsoft Graph (`Application.ReadWrite.All`
   or `Application.ReadWrite.OwnedBy` with ownership of the persistent `cf-idp-azure-reader` app).
   Azure CLI selection is checked separately. No Reader PEM or personal browser login is required.
   The GitHub Actions app authenticates by OIDC, never with either certificate. Keep the existing Cloudflare Terraform
   root at `~/projects/cloudflare/amirasyraf.dev`; `poc` applies only the portal record there.
6. `--gha` pulls the public [`vendy-idp` package](https://github.com/users/amirasyraf/packages/container/package/vendy-idp)
   anonymously. If using a private package, set the dedicated pull credential below instead.

| Identity or credential | What it does |
|---|---|
| Portal username and password | Signs a user in; catalog membership controls portal access. |
| Laptop `gh` login and Git SSH credentials | Inspect and manage workflows and repo settings, clone, and push as the operator. |
| Backstage's fine-grained PAT (`GITHUB_TOKEN` in `.poc.env`) | Reads platform inventory, templates and configuration, and dispatches/observes stages. It does not write inventory. |
| GitHub Actions' job `GITHUB_TOKEN` | Checks out and writes inventory in `vendy-platform` using `contents: write`. The displayed commit author is not a GitHub App. |
| AWS/Azure GitHub OIDC | Grants cloud access to workflow jobs. This is unrelated to GitHub OAuth sign-in. |
| Static workflow `BACKSTAGE_TOKEN` | Lets the stage job refresh catalog entries and check demo cleanup eligibility. It cannot read task or VM run state. |
| `GHCR_PULL_USER` / `GHCR_PULL_TOKEN` | Export both for a private GHCR image. Use a separate classic PAT with only `read:packages` and access to `vendy-idp`. The host uses a temporary Docker config; the token does not enter Actions, `.poc.env`, or Backstage. Omit both for an explicitly public package. |
| IDP EC2 instance role | Reads scoped AWS secrets, observes EC2, and checks golden AMI availability with `ec2:DescribeImages`; the Azure Reader secret is used for read-only Azure calls. |

Full `up` generates a 364-day Reader certificate and writes the private key directly to
`compute-factory/azure-reader` before registering the public certificate in Entra. Existing keys
are preserved and a resumed bootstrap reuses the current secret. The portal and host-side
autoscaler read that secret through the EC2 role; no new Reader key goes into Terraform state
or a laptop file. Run lifecycle operations from one host at a time; don't edit the Reader app's
Entra credentials concurrently. There is no automatic rotation. To replace an expired cycle
credential, tear down and start a new cycle. `down --keep-images` also revokes the Reader
certificate; it only keeps the images and their SSH CA. Any manually installed Reader certificates
predating this flow (and their old local files or state backups) aren't removed automatically.

The public platform repo contains VM inventory and its history, including request metadata and public
addresses. Don't enter secrets or real sensitive data in a POC request. Terraform state and its
backups can contain secret material; don't copy them into tickets, logs, or Git.

## Diagnose before retrying

| Symptom | Check and next action |
|---|---|
| Bootstrap failed before a bake was confirmed | Read `./poc status` and `.generated/logs/`. If dispatch returned ambiguously, find the exact run in builder Actions first; don't dispatch it again based on missing output. |
| A bake run exists but monitoring stopped | `./poc watch-bake <run-id>` is read-only. Exit `2` means unknown, not failed; it does not cancel the run. Exit `0` proves the GitHub conclusion, not artifact usability. |
| A gated `up --gha` was interrupted | Inspect `.generated/logs/idp-actions.log` and resume with `./poc up --gha --resume`. It uses the checkpoint for the existing instance, attaches to the original IDP run and either re-verifies retained images or attaches to the saved bake. Supply the exact bake ID only when dispatch was ambiguous; do not assume the latest run is yours. Neither path reapplies Terraform. |
| An Actions IDP build or image pull failed | Check the reported run and `idp.log`. A failed build can be rerun on GitHub (the same run ID); a missing artifact or package is not grounds to fall back to `latest`. Check registry visibility or the dedicated read-only credential before resuming. |
| Bootstrap stopped (or was interrupted) after an exact bake was dispatched, and that bake is running or succeeded | `./poc up --resume <run-id>` bootstraps the IDP while it waits for the bake, then verifies the original bake artifacts, current matching images, owned running host, empty VM state/inventory, and idle workflows. It does not reapply Terraform or rebake. |
| Terraform partially applied, or the bake actually failed | Inspect `images-apply.log`, `infra-apply.log`, or `bake.log` in `.generated/logs/` as applicable. Resume is not a general repair or rerun command. Preserve state and identify the failed step first. |
| Azure Reader provisioning or revocation failed | Check the operator certificate and Graph app permissions. Do not upload a replacement Reader PEM. When eligible, a gated bootstrap reuses the current secret on resume; with `--gha`, resume also works after golden-image reuse. If bootstrap cannot resume, run `down --keep-images` only when ready to start a new cycle. Rerun `down` with the same retention option if Graph revocation could not be verified. |
| Portal inaccessible or app release failed | For a bootstrap, inspect `.generated/logs/idp.log` and `dns.log`. On an existing host, `./poc logs caddy 30m` and `./poc logs backstage 30m` separate DNS/TLS/proxy failures from backend readiness and catalog errors. A routine release does not reapply DNS. |
| An image says **Not available** | Confirm the latest matching AMI or eligible Azure gallery version is usable (including Azure replication). If the image exists, check the IDP backend's `Image availability` warning for a cloud read failure. A new launch request is also refused when availability cannot be verified. |
| Task page is blank or stuck | `./poc task <task-uuid>` shows the stored task and events. If events exist, inspect `/events` responses, Caddy access logs and `Browser ...` entries. If a deploy just ran, check for the new-version banner or `Browser stale-bundle`. |
| VM run stops advancing | `./poc run <run-id>` shows the stage, attempt, next retry and GitHub run ID. Follow that exact stage in Actions; `Validate inputs` failures won't auto-retry. Use the portal's Retry/Skip when offered; a separate manual dispatch is not tracked by that run. |
| Portal inventory is stale | Check the file on platform `main`, the stage's refresh result, and the catalog location. Git is the desired-state inventory, while cloud-observed power state and per-stage progress come from the IDP. Editing inventory directly does not reconcile a VM. |
| A container is stuck in "Provisioning capacity" or fails | The run page shows the waiting reason or the image/crash error. On the host (approved access), `sudo k3s kubectl -n kube-system logs deploy/cluster-autoscaler-aws` (or `-azure`) shows scale-up decisions; `sudo k3s kubectl get nodes -L vendy.io/pool` shows joined workers. A worker that never joins: its cloud-init log (`/var/log/cloud-init-output.log`). |
| A container URL has no certificate or returns 502 | `./poc logs caddy 30m`: on-demand TLS is refused unless `/api/containers/tls-ask` answers 200 (public container with a Service). 502 means Caddy could not reach `<name>.apps.svc.cluster.local` through the cluster DNS or the pod is not ready. |
| Teardown says `NOT CLEAN` | Read the named log and leftover or lock warnings. Fix the blocker and rerun with the same retention option. Do not clear state, delete the bucket by hand, or treat a failed lookup as proof of absence. |

`./poc` saves the confirmed bake ID in `.generated/bake-run-id`. The monitor checks every 30 seconds
and gives up after two hours or ten consecutive failed reads. If plain bootstrap finds reusable
images from an older builder commit, it warns rather than rebuilding them. Use the images-only
command after validating a changed recipe if you need the new version.

The task query reads `tasks` and `task_events` in `backstage_plugin_scaffolder`. The run query reads
`vm_runs` and `vm_run_stages` in `backstage_plugin_vms`. Start with these commands instead of ad hoc
host access. If SSH is needed, the stack lives at `/opt/idp`; use the approved host-access procedure.
When running `docker compose exec -T` inside an SSH here-document, redirect its stdin from
`/dev/null` so it doesn't consume the rest of the remote script.

### State that Git can't restore

A routine deploy retains the host's Postgres volume; full teardown does not. Git holds VM inventory,
not task events, attempts, retry deadlines, or demo settings. The POC has no automated Postgres
restore procedure. If the database is lost while VMs remain, preserve the volume and cloud state
and reconcile existing Actions runs and VMs before resuming dispatch or automatic cleanup.

The SSH CA and images are one tier. `down --keep-images` retains both. A clean cycle without image
retention creates a new CA and rebakes; old certificates won't work with the new images. Don't
rotate the signing key in place while existing guests trust the old public key. The POC doesn't
automate that migration, and rebaking only changes new guests.

Treat `./poc status` as a snapshot, not proof of a completed teardown or a safe time to bypass
workflow gates. It displays recent runs best-effort; use the lifecycle command's verified `CLEAN`
or `CLEAN (images retained)` result after an intentional teardown.

## Verification by change type

| Change | Checks |
|---|---|
| Documentation only | Verify examples, commands, and contracts against code; check links and Markdown diffs. No live startup needed. |
| Portal or backend | Use the [IDP checks](https://github.com/amirasyraf/vendy-idp#checks). Deploy the tested image with `./poc deploy-idp <image-id>` or `./poc deploy-idp --gha [<run-id>]`, then sign in and run the live checks. |
| VM workflow, schema, or Ansible | Run the [platform's offline integration and static checks](https://github.com/amirasyraf/vendy-platform#extending-and-checking). Exercise changed guest commands on equivalent disposable guests before any stage dispatch. |
| Golden images | Run the [builder's full validation and guest preflight](https://github.com/amirasyraf/vendy-builder#local-checks) before Actions. `./poc up --images-only` always dispatches a bake and may remove superseded images after validating the new matrix. |

For a live test, use disposable requests and record the OS/cloud pairs actually tested. Verify
sign-in, Launch → image → form, run and inventory updates, owning-team access and denial in the
portal for another team, SSH and Windows RDP as applicable, then destroy only the test VMs. Exercise
the specific day-2 operation if it changed. Do not tear down the whole POC just to verify a release.
