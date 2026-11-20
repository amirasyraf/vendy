"""./poc up, up --resume, up --images-only and down (DESIGN.md §12)."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
from concurrent.futures import Future
from concurrent.futures import wait as wait_futures
from pathlib import Path

from .cloud import (
    delete_image_versions, destroy_vms, prepare_vm_states, remove_empty_state_bucket, state_bucket_keys, stop_idp,
    sweep_azure, sweep_infra_orphans, sweep_tagged, verify_clean, verify_cloud_clean, verify_images_state_empty,
    verify_infra_state_empty,
)
from .core import (
    AWS_REGION, AZURE_READER_PEM, BUILDER_REPO, CA_FINGERPRINT_RE, CF_DNS_DIR, DOMAIN, GEN, IMAGES_DIR, INFRA_DIR, LOGS,
    MAINTENANCE_VARIABLE, OWNER, PLATFORM_REPO, POC_ENV, SSH_KEY, STATE_BUCKET, USERS_FILE, WORKFLOWS, Unknown,
    cancel_background, die,
    gh_read, images_apply_or_destroy, infra_apply_or_destroy, info, last_line, log, nonempty, poc_env_github_token,
    require, require_image_tools, require_split_layout, require_terraform_azure_auth, require_tools, root_state, run,
    show_tail, spawn, success, tf, tf_output, warn,
)
from .github import (
    active_runs, cancel_runs, check_repo_variables, clear_platform_wiring, ensure_repos, reset_inventory,
    set_maintenance_gate, verify_frozen, verify_inventory_empty, verify_maintenance_gate, verify_quiescent,
    verify_wiring_cleared, workflow_action,
)
from .idp import deploy_idp, dns_apply, existing_idp_ip
from .images import (
    apply_images_root, ca_fingerprint_from_ssm, current_images, image_tier_lines, report_images, run_bake,
    verify_bake_images, watch_bake,
)

IPV4 = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+")
BAKE_SPEC = ((BUILDER_REPO, "bake.yml"),)


def _clone_idp(target: Path) -> bool:
    result = run(["git", "clone", "-q", "--depth", "1", "--branch", "main", f"git@github.com:{OWNER}/vendy-idp.git", str(target)])
    if not result.ok:
        warn(f"could not clone vendy-idp main: {last_line(result.err)}")
    return result.ok


def _up_preflight() -> None:
    require_image_tools("docker", "scp")
    for check in (["docker", "info"], ["docker", "buildx", "version"], ["docker", "compose", "version"]):
        if not run(check).ok:
            die("Docker, buildx and Compose must be usable")
    if not CF_DNS_DIR.is_dir() or not os.access(CF_DNS_DIR / "idp.tf", os.R_OK):
        die(f"missing Cloudflare IDP Terraform configuration: {CF_DNS_DIR}")
    if not os.access(SSH_KEY, os.R_OK) or not os.access(f"{SSH_KEY}.pub", os.R_OK):
        die("missing IDP SSH key pair")
    if not os.access(POC_ENV, os.R_OK):
        die(f"missing {POC_ENV} (copy poc.env.example and fill it in)")
    if not poc_env_github_token():
        die(".poc.env must set GITHUB_TOKEN")
    if not nonempty(USERS_FILE):
        die("no IDP users; run ./poc set-password <user> first")
    # One-time Azure setup that survives down (DESIGN.md section 9).
    if not nonempty(AZURE_READER_PEM):
        die(f"missing {AZURE_READER_PEM} (IDP Azure reader certificate; DESIGN.md section 9)")


def cmd_up(resume: str | None) -> None:
    log("Preflight")
    _up_preflight()
    check_repo_variables(BUILDER_REPO, PLATFORM_REPO)
    require(tf(INFRA_DIR, "init", "-input=false", "-lockfile=readonly"), "shared Terraform init failed")
    try:
        state = root_state(INFRA_DIR)
    except Unknown:
        die("cannot read local Terraform state; refusing to apply")
    if resume is None and state:
        die("infra state already exists; to finish bootstrap after a bake was dispatched use ./poc up --resume <run-id>. "
            "Do not tear down to resume.")
    if resume is not None and not state:
        die("no existing infra to resume; use ./poc up for a clean start")
    if verify_inventory_empty() != "empty":
        die("platform main has a nonempty or unreadable VM inventory; refusing to reset it during up")
    ensure_repos()
    tmp = Path(tempfile.mkdtemp(dir=GEN, prefix="up."))
    stop = threading.Event()
    pending: list[Future] = []
    try:
        # Before any mutation: bootstrap cannot succeed without it.
        if not _clone_idp(tmp / "idp"):
            die("could not clone vendy-idp main; nothing was changed")
        bake, bake_log = None, LOGS / "bake.log"
        images = None

        if resume is not None:
            log("Check existing bootstrap")
            if not verify_maintenance_gate("1", PLATFORM_REPO):
                die("platform is not gated; cannot resume a running POC")
            # The resumed bake may still be running; nothing else may be.
            try:
                others = [run_ref for run_ref in active_runs() if run_ref != f"{BUILDER_REPO}:{resume}"]
            except Unknown:
                die("cannot read resource workflow runs")
            if others:
                die(f"resource workflow runs other than bake {resume} are active: {' '.join(others)}")
            ip = existing_idp_ip()
            bucket = tf_output(INFRA_DIR, "state_bucket")
            if bucket != STATE_BUCKET:
                die("unexpected state bucket in Terraform outputs")
            try:
                keys = state_bucket_keys(require_present=True)
            except Unknown:
                keys = None
            if keys is None or keys:
                die("state bucket is missing, nonempty or unreadable; cannot resume bootstrap")
            fp = tf_output(IMAGES_DIR, "ssh_ca_fingerprint") or ""
            if not CA_FINGERPRINT_RE.fullmatch(fp) or ca_fingerprint_from_ssm() != fp:
                die("cannot verify the existing POC SSH CA")
        else:
            log("Keep resource workflows closed during bootstrap")
            set_maintenance_gate("1", BUILDER_REPO, PLATFORM_REPO)
            workflow_action("disable", WORKFLOWS)
            if not verify_quiescent():
                die("cannot verify bootstrap freeze and workflow quiescence")
            # Retried like down's final check: right after a down, cached DNS and stale tag-index
            # entries can fail a single pass. Quiescence is rechecked around every attempt.
            if not verify_clean(True):
                die("cannot prove a clean cloud/DNS starting point (only the SSH CA and golden images may remain); "
                    "inspect the checks above")

            log("Image tier (SSH CA, Azure gallery)")
            fp = apply_images_root() or die(f"image tier not ready (see {LOGS / 'images-apply.log'})")
            info(f"SSH CA {fp}")
            try:
                images = current_images(fp, "main")
            except Unknown:
                die("image availability is unknown; no bake was dispatched and shared infra was not applied. Retry ./poc up.")
            if images is not None:
                info("reusing images that trust this CA; no bake")

            log("Shared AWS infra (state bucket, IDP host, tokens)")
            if not infra_apply_or_destroy("apply", log=LOGS / "infra-apply.log").ok:
                show_tail(LOGS / "infra-apply.log", 40)
                die("infra apply failed")
            ip, bucket = tf_output(INFRA_DIR, "idp_ip") or "", tf_output(INFRA_DIR, "state_bucket")
            if not IPV4.fullmatch(ip) or bucket != STATE_BUCKET:
                die("infra returned an unexpected host or state bucket")
        info(f"IDP host {ip}, state bucket {bucket}")

        # The IDP does not consume golden images: bake (or watch a resumed bake) while the IDP bootstraps.
        # The platform gate stays closed until both succeed.
        log("Golden images")
        if resume is not None:
            bake_log = LOGS / "bake-resume.log"
            info(f"verifying bake {resume} in the background; live log: {bake_log}")
            bake = spawn(lambda: watch_bake(resume, stop) == 0 and verify_bake_images(resume, fp, prune=False, stop=stop),
                         log_path=bake_log)
        elif images is None:
            set_maintenance_gate("0", BUILDER_REPO)
            if not verify_maintenance_gate("0", BUILDER_REPO):
                die("builder maintenance gate did not open")
            workflow_action("enable", BAKE_SPEC)
            info(f"baking every images.yaml row in the background; live log: {bake_log}")
            bake = spawn(lambda: run_bake(fp, stop), log_path=bake_log)
        else:
            report_images(images)
        if bake is not None:
            pending.append(bake)

        log("GitHub wiring")
        require(run(["gh", "variable", "set", "TF_STATE_BUCKET", "-R", PLATFORM_REPO, "-b", bucket]), "could not set TF_STATE_BUCKET")
        require(run(["gh", "variable", "set", "BACKSTAGE_URL", "-R", PLATFORM_REPO, "-b", f"https://{DOMAIN}"]),
                "could not set BACKSTAGE_URL")
        token = tf_output(INFRA_DIR, "backstage_token") or die("could not read the Backstage token output")
        require(run(["gh", "secret", "set", "BACKSTAGE_TOKEN", "-R", PLATFORM_REPO], input=token), "could not set BACKSTAGE_TOKEN")
        set_maintenance_gate("0", BUILDER_REPO)
        if not verify_maintenance_gate("0", BUILDER_REPO):
            die("builder maintenance gate did not open")
        workflow_action("enable", BAKE_SPEC)

        log("Bootstrap IDP")
        if not verify_maintenance_gate("1", PLATFORM_REPO):
            die("platform gate changed; refusing to publish the IDP")
        idp_log = LOGS / "idp.log"
        info(f"live log: {idp_log}")
        deploy = spawn(lambda: deploy_idp(ip, "bootstrap", None, tmp / "idp"), log_path=idp_log)
        pending.append(deploy)
        idp_ok = deploy.result()
        if not idp_ok:
            show_tail(idp_log, 35)
            warn("IDP bootstrap failed; the platform remains gated. Inspect the log before retrying.")
        else:
            info("IDP healthy")

        bake_ok = True
        if bake is not None:
            log("Wait for golden images")
            info(f"live log: {bake_log}")
            try:
                bake_ok = bool(bake.result())
            except Exception as exc:  # noqa: BLE001 - report any failure of the background job
                warn(f"bake verification stopped unexpectedly: {exc}")
                bake_ok = False
            if not bake_ok:
                show_tail(bake_log, 30)
                warn("bake not verified; platform remains gated. Check the exact bake run; after success use "
                     "./poc up --resume <run-id>.")
            else:
                try:
                    images = current_images(fp, "main")
                except Unknown:
                    images = None
                if images is None:
                    bake_ok = False
                    warn("could not verify current images for builder main; platform remains gated. Use ./poc up --resume <run-id>.")
                else:
                    report_images(images)
        if not (idp_ok and bake_ok):
            die("bootstrap incomplete; the platform remains gated")

        log("Enable VM stages")
        workflow_action("enable", ((PLATFORM_REPO, "stage.yml"),))
        set_maintenance_gate("0", PLATFORM_REPO)
        if not verify_maintenance_gate("0", PLATFORM_REPO):
            die("platform maintenance gate did not open")

        log("Up")
        info(f"IDP:          https://{DOMAIN}")
        info(f"SSH CA:       {fp}")
        info(f"IDP host:     ssh ubuntu@{ip}")
    except BaseException:
        # Stop background work before joining it; the bake itself continues on GitHub.
        cancel_background()
        raise
    finally:
        stop.set()
        wait_futures(pending)
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_images_only() -> None:
    """Bake and verify every images.yaml row, whether the POC is up or down. The builder gate and bake
    workflow are returned to their previous state afterwards, also on failure."""
    log("Preflight")
    require_image_tools()
    check_repo_variables(BUILDER_REPO)
    gate = gh_read("variable", "get", MAINTENANCE_VARIABLE, "-R", BUILDER_REPO)
    if not gate.ok:
        die("could not read the builder gate")
    previous_gate = gate.out.strip()
    if previous_gate not in ("0", "1"):
        die(f"builder {MAINTENANCE_VARIABLE} is '{previous_gate}'; run up or down first")
    state = gh_read("api", f"repos/{BUILDER_REPO}/actions/workflows/bake.yml", "--jq", ".state")
    if not state.ok:
        die("could not read bake.yml state")
    previous_workflow = state.out.strip()
    if previous_workflow not in ("active", "disabled_manually"):
        die(f"unexpected bake.yml state: {previous_workflow}")

    log("Image tier (SSH CA, Azure gallery)")
    fp = apply_images_root() or die(f"image tier not ready (see {LOGS / 'images-apply.log'})")
    info(f"SSH CA {fp}")

    log("Bake every images.yaml row")
    succeeded = False
    try:
        set_maintenance_gate("0", BUILDER_REPO)
        if not verify_maintenance_gate("0", BUILDER_REPO):
            die("builder maintenance gate did not open")
        workflow_action("enable", BAKE_SPEC)
        bake_log = LOGS / "bake.log"
        info(f"live log: {bake_log}")
        if not spawn(lambda: run_bake(fp), log_path=bake_log).result():
            show_tail(bake_log, 30)
            die(f"bake not verified; see {bake_log}")
        try:
            images = current_images(fp, "main")
        except Unknown:
            images = None
        if images is None:
            die("baked images are not current for builder main")
        log("Images")
        report_images(images)
        succeeded = True
    except BaseException:
        cancel_background()  # the main thread still restores the gate below
        raise
    finally:
        log(f"Restore builder gate ({MAINTENANCE_VARIABLE}={previous_gate}, bake.yml {previous_workflow})")
        if previous_workflow == "disabled_manually":
            workflow_action("disable", BAKE_SPEC)
        if previous_gate == "1":
            set_maintenance_gate("1", BUILDER_REPO)
            if not verify_maintenance_gate("1", BUILDER_REPO):
                die("could not restore the builder gate")
            if not succeeded:
                warn("if the bake is still running, ./poc down cancels it and sweeps its build resources")


def _destroy_root(label: str, root: Path, init_log: str, destroy_log: str, destroy, needs_azure: bool,
                  azure_ok: bool = True) -> bool:
    """Init and destroy one Terraform root when its state is nonempty. True when a destroy was attempted
    (so its state can be reconciled again after the fallback sweeps)."""
    if not tf(root, "init", "-input=false", "-lockfile=readonly", log=LOGS / init_log).ok:
        warn(f"{label} Terraform init failed (see {LOGS / init_log}); using the owned-resource sweep")
        return False
    try:
        state = root_state(root)
    except Unknown:
        state = []
    if not state:
        return True
    if needs_azure and not azure_ok:
        return False
    if not destroy("destroy", log=LOGS / destroy_log).ok:
        show_tail(LOGS / destroy_log, 30)
        warn(f"{label} Terraform destroy failed; sweeping independently and then retrying the local state")
    return True


def cmd_down(keep_images_requested: bool) -> None:
    log("Preflight")
    require_tools()
    require_split_layout()
    workers_text = os.environ.get("PARALLEL_DESTROYS") or "5"
    if not re.fullmatch(r"[1-9][0-9]?", workers_text) or int(workers_text) > 20:
        die("PARALLEL_DESTROYS must be between 1 and 20")
    workers = int(workers_text)
    keep_bucket, keep_images = False, keep_images_requested
    azure_ok = versions_ok = dns_ok = infra_tf = images_tf = False
    tmp = Path(tempfile.mkdtemp(dir=GEN, prefix="down."))
    platform_dir = tmp / "platform"
    try:
        log("Freeze: gate resource workflows and stop the owned IDP")
        set_maintenance_gate("1", BUILDER_REPO, PLATFORM_REPO)
        workflow_action("disable", WORKFLOWS)
        if not verify_frozen():
            die("NOT CLEAN: cannot verify teardown freeze")
        cancel_runs()
        if not verify_quiescent():
            die("NOT CLEAN: cannot verify resource workflow quiescence")
        if not stop_idp():
            warn("could not stop every owned IDP; workflows are frozen and the owned AWS sweep will attempt termination")
        plan = prepare_vm_states(tmp / "locks")
        if plan is None:
            die("NOT CLEAN: cannot establish state and lock safety; no resource sweep was attempted")

        log(f"Destroy user VMs (Terraform, {workers} in parallel)")
        clone = run(["git", "clone", "-q", "--depth", "1", "--branch", "main", f"git@github.com:{PLATFORM_REPO}.git", str(platform_dir)])
        if not clone.ok:
            warn("could not check out canonical platform main; VM state and inventory will not be discarded")
        if not destroy_vms(plan, tmp / "provider-mirror", platform_dir, workers) or not remove_empty_state_bucket():
            keep_bucket = keep_images = True
            warn(f"preserving {STATE_BUCKET} and golden images for VM recovery; continuing to clean owned compute resources")
        if not verify_quiescent():
            die("NOT CLEAN: the resource workflows are no longer quiescent")
        if keep_bucket and prepare_vm_states(tmp / "locks") is None:
            die("NOT CLEAN: state or lock safety changed; refusing to sweep potentially active operations")

        log(f"Sweep owned AWS leftovers and Azure VM/build groups; remove DNS {DOMAIN}")
        aws_sweep = spawn(lambda: sweep_tagged(True, keep_images))
        dns = spawn(lambda: dns_apply(""))
        if sweep_azure(True, True):
            azure_ok = True
        else:
            warn("Azure VM/build groups remain; the image tier is preserved")
        if azure_ok and not keep_images:
            if delete_image_versions():
                versions_ok = True
            else:
                warn("Azure gallery versions remain; the SSH CA and gallery are preserved until they are reconciled")
        if not aws_sweep.result():
            warn("some owned AWS resources could not be swept; a final sweep and verification follow")
        dns_ok = dns.result()
        if not dns_ok:
            warn(f"DNS removal failed (see {LOGS / 'dns.log'})")

        # The two roots share no Terraform references (infra/ names the CA secret only), so they are
        # destroyed concurrently.
        log("Destroy shared infra" + (" and image tier (SSH CA, Azure gallery)" if versions_ok else ""))
        # Set the Azure environment before the threads start: os.environ must not change while
        # another thread copies it for a subprocess.
        azure_auth = require_terraform_azure_auth() if versions_ok else False
        infra = (spawn(lambda: _destroy_root("shared", INFRA_DIR, "infra-init.log", "infra-destroy.log",
                                             infra_apply_or_destroy, False))
                 if not keep_bucket else None)
        images = (spawn(lambda: _destroy_root("image tier", IMAGES_DIR, "images-init.log", "images-destroy.log",
                                              images_apply_or_destroy, True, azure_auth))
                  if versions_ok else None)
        infra_tf = bool(infra and infra.result())
        images_tf = bool(images and images.result())
        if not versions_ok and keep_images_requested:
            info("keeping the image tier (--keep-images)")

        aws_sweep = spawn(lambda: sweep_tagged(False, keep_images))
        if azure_ok and not sweep_azure(False, not versions_ok):
            azure_ok = False
            warn("final Azure sweep failed")
        if not aws_sweep.result():
            warn(f"final AWS {AWS_REGION} sweep failed")
        if not sweep_infra_orphans(versions_ok):
            warn("some shared AWS fallbacks failed")
        if infra_tf and not verify_infra_state_empty():
            if not infra_apply_or_destroy("destroy", log=LOGS / "infra-destroy.log", append=True).ok:
                warn(f"shared Terraform state still needs recovery (see {LOGS / 'infra-destroy.log'})")
        if images_tf and not verify_images_state_empty():
            if not images_apply_or_destroy("destroy", log=LOGS / "images-destroy.log", append=True).ok:
                warn(f"image tier Terraform state still needs recovery (see {LOGS / 'images-destroy.log'})")

        # Requested retention only needs the VM/build groups gone; full teardown also needs the gallery versions.
        images_done = azure_ok if keep_images_requested else versions_ok
        if keep_bucket or not azure_ok or not images_done or not dns_ok:
            log("Report remaining cloud/DNS resources")
            verify_cloud_clean(keep_images_requested)
            die("NOT CLEAN: teardown retained recovery state or could not complete Azure/DNS cleanup. "
                "Fix the reported blocker and rerun down.")
        log("Verify cloud, DNS and local state")
        if not verify_clean(keep_images_requested):
            die("NOT CLEAN: teardown verification failed; see the check-specific warnings above")

        log("Clear and verify GitHub wiring and inventory")
        if not clear_platform_wiring():
            die("NOT CLEAN: platform GitHub wiring could not be cleared and verified")
        inventory = verify_inventory_empty()
        if inventory == "empty":
            info("inventory already empty")
        elif inventory == "nonempty":
            if not (platform_dir / ".git").is_dir() or not reset_inventory(platform_dir):
                die("NOT CLEAN: could not reset and verify platform main's VM inventory")
        else:
            die("NOT CLEAN: could not read platform main's VM inventory; it was not reset. Rerun down to verify.")
        if not verify_quiescent():
            die("NOT CLEAN: resource workflows are no longer quiescent")
        if not verify_wiring_cleared():
            die("NOT CLEAN: platform GitHub wiring remains or could not be read")
        if keep_images_requested:
            log("Kept image tier")
            for line in image_tier_lines():
                info(line)
            success("CLEAN (images retained): POC cloud resources, DNS, Terraform state, GitHub wiring and inventory "
                    "verified; the SSH CA and golden images above remain.")
        else:
            success("CLEAN: POC cloud resources, DNS, Terraform state, GitHub wiring and inventory verified.")
    except BaseException:
        cancel_background()
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
