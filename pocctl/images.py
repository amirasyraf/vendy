"""Image tier: the SSH CA, golden image availability, bakes and their verification."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import NamedTuple

from .core import (
    AWS_REGION, AZURE_SUBSCRIPTION, BUILDER_REPO, CA_FINGERPRINT_RE, GEN, IMAGES_DIR, LOGS, PLATFORM_REPO,
    Unknown, aws, aws_json, az, az_json, delete_aws, echo, gh_read, images_apply_or_destroy, info, last_line,
    parallel, pause, run, show_tail, tf, tf_output, wait_seconds, warn,
)

ROW_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
GALLERY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
BUILDERS = {"aws": "amazon-ebs", "azure": "azure-arm"}
BAKE_WATCH_SECONDS = 7200


class Image(NamedTuple):
    platform: str
    os: str
    version: str
    id: str
    sha: str
    created: str


def ca_fingerprint_from_ssm() -> str | None:
    key = aws("ssm", "get-parameter", "--name", "/compute-factory/ssh-ca-pub", "--query", "Parameter.Value", "--output", "text")
    if not key.ok:
        return None
    result = run(["ssh-keygen", "-lf", "/dev/stdin"], input=key.out.strip() + "\n")
    fields = result.out.split()
    fp = fields[1] if result.ok and len(fields) > 1 else ""
    return fp if CA_FINGERPRINT_RE.fullmatch(fp) else None


def apply_images_root() -> str | None:
    """Create the CA and gallery if missing (otherwise a no-op) and return the CA fingerprint. The bake
    reads the CA from SSM, so require SSM to hold exactly the key in this root's state."""
    init_log, apply_log = LOGS / "images-init.log", LOGS / "images-apply.log"
    if not tf(IMAGES_DIR, "init", "-input=false", "-lockfile=readonly", log=init_log).ok:
        show_tail(init_log)
        warn("image tier Terraform init failed")
        return None
    if not images_apply_or_destroy("apply", log=apply_log).ok:
        show_tail(apply_log, 40)
        warn("image tier apply failed")
        return None
    fp = tf_output(IMAGES_DIR, "ssh_ca_fingerprint")
    if not fp or not CA_FINGERPRINT_RE.fullmatch(fp):
        warn("image tier returned no valid CA fingerprint")
        return None
    ssm = ca_fingerprint_from_ssm()
    if ssm != fp:
        warn(f"SSM /compute-factory/ssh-ca-pub ({ssm or 'unreadable'}) does not match the CA in state ({fp})")
        return None
    return fp


def image_rows(ref: str) -> list[tuple[str, str, str]]:
    """(os, version, platform) for each images.yaml row at a builder ref, typed as the bake matrix sees them."""
    import yaml

    result = gh_read("api", "-H", "Accept: application/vnd.github.raw", f"repos/{BUILDER_REPO}/contents/images.yaml?ref={ref}")
    if not result.ok:
        raise Unknown(f"could not fetch images.yaml at builder {ref}")
    try:
        rows = yaml.safe_load(result.out)
    except yaml.YAMLError as exc:
        raise Unknown(f"images.yaml at builder {ref} is not valid YAML: {exc}") from None
    if not isinstance(rows, list) or not rows:
        raise Unknown("images.yaml has no rows")
    parsed = []
    for row in rows:
        values = tuple(str(row.get(key, "")) for key in ("os", "version", "platform")) if isinstance(row, dict) else ()
        if len(values) != 3 or not all(ROW_VALUE.fullmatch(v) for v in values):
            raise Unknown(f"unexpected images.yaml row: {row}")
        parsed.append((values[0], values[1], values[2]))
    return parsed


def azure_gallery_image(os_name: str, version: str) -> str:
    """The gallery definition a release launches from (the platform repo's mapping, as Terraform uses it)."""
    result = gh_read("api", "-H", "Accept: application/vnd.github.raw",
                     f"repos/{PLATFORM_REPO}/contents/terraform/platforms/azure/platform.json?ref=main")
    if not result.ok:
        raise Unknown("could not read platform.json")
    try:
        name = json.loads(result.out)["os"][os_name]["galleryImages"][version]
    except (ValueError, KeyError, TypeError):
        name = None
    if not isinstance(name, str) or not GALLERY_NAME.fullmatch(name):
        raise Unknown(f"could not resolve Azure gallery definition for {os_name} {version}")
    return name


def current_image(platform: str, os_name: str, version: str, fp: str) -> tuple[str, str, str] | None:
    """(id, git sha, created) of the image a new VM of this row would launch (the newest, as platform
    Terraform selects it) if it is usable and trusts CA fp; None if not; Unknown if undeterminable."""
    if platform == "aws":
        data = aws_json("ec2", "describe-images", "--owners", "self", "--filters", f"Name=tag:cf:os,Values={os_name}",
                        f"Name=tag:cf:os-version,Values={version}", "Name=tag:cf:managed,Values=true")
        images = data.get("Images") if isinstance(data, dict) else None
        if not isinstance(images, list):
            raise Unknown("invalid Images response")
        if not images:
            return None
        latest = sorted(images, key=lambda image: image.get("CreationDate") or "")[-1]
        tags = {t.get("Key"): t.get("Value") for t in latest.get("Tags") or []}
        if latest.get("State") == "available" and tags.get("cf:poc") == "true" and tags.get("cf:ssh-ca-fingerprint") == fp:
            return latest["ImageId"], tags.get("cf:git-sha") or "unknown", latest.get("CreationDate") or ""
        return None
    if platform == "azure":
        definition = azure_gallery_image(os_name, version)
        versions = az_json("sig", "image-version", "list", "-g", "cf-images", "-r", "cfimages", "-i", definition)
        if not isinstance(versions, list):
            raise Unknown("invalid gallery response")
        candidates = [v for v in versions if (v.get("publishingProfile") or {}).get("excludeFromLatest") is not True]
        if not candidates:
            return None
        try:
            latest = sorted(candidates, key=lambda v: [int(part) for part in v["name"].split(".")])[-1]
        except (KeyError, ValueError, AttributeError):
            raise Unknown(f"unexpected gallery version name in {definition}") from None
        tags = latest.get("tags") or {}
        if (latest.get("provisioningState") == "Succeeded" and tags.get("cf:poc") == "true"
                and tags.get("cf:ssh-ca-fingerprint") == fp and tags.get("cf:os") == os_name
                and tags.get("cf:os-version") == version):
            published = (latest.get("publishingProfile") or {}).get("publishedDate") or "unknown"
            return latest["id"], tags.get("cf:git-sha") or "unknown", published
        return None
    raise Unknown(f"./poc cannot verify images for platform {platform}")


def current_images(fp: str, ref: str) -> list[Image] | None:
    """Every images.yaml row's current usable image trusting CA fp, or None if any row has none.
    Raises Unknown when availability cannot be established (not safe to dispatch a bake)."""
    try:
        rows = image_rows(ref)
    except Unknown as exc:
        warn(f"could not read images.yaml at builder {ref}: {exc}")
        raise

    def lookup(row: tuple[str, str, str]) -> tuple[str, str, str] | None | Unknown:
        os_name, version, platform = row
        try:
            return current_image(platform, os_name, version, fp)
        except Unknown as exc:
            return exc

    images, missing = [], False
    for (os_name, version, platform), found in zip(rows, parallel(lookup, rows)):
        if isinstance(found, Unknown):
            warn(f"could not determine the current {platform} {os_name} {version} image ({found}); not safe to dispatch a bake")
            raise found
        if found is None:
            info(f"no current {platform} {os_name} {version} image trusting CA {fp}")
            missing = True
        else:
            images.append(Image(platform, os_name, version, *found))
    return None if missing else images


def report_images(images: list[Image]) -> None:
    """Warn only: kept images are used even when builder main has moved on."""
    result = gh_read("api", f"repos/{BUILDER_REPO}/commits/main", "--jq", ".sha")
    main = result.out.strip() if result.ok else ""
    if not result.ok:
        warn("could not read builder main")
    for image in images:
        row = f"{image.platform} {image.os} {image.version}"
        info(f"{row}: {image.id} (built {image.created} from builder {image.sha[:7]})")
        if main and image.sha != main:
            warn(f"{row} image is from builder {image.sha[:7]}, main is {main[:7]}; run ./poc up --images-only to rebake")


def prune_superseded(platform: str, os_name: str, version: str, keep: str) -> bool:
    """After a verified bake, remove every other image of the same row so only the current one remains."""
    ok = True
    try:
        if platform == "aws":
            data = aws_json("ec2", "describe-images", "--include-disabled", "--owners", "self", "--filters",
                            "Name=tag:cf:poc,Values=true", "Name=tag:cf:managed,Values=true",
                            f"Name=tag:cf:os,Values={os_name}", f"Name=tag:cf:os-version,Values={version}")
            for image in data.get("Images") or []:
                ami = image.get("ImageId", "")
                if not re.fullmatch(r"ami-[0-9a-f]+", ami) or ami == keep:
                    continue
                if not delete_aws(f"superseded AMI {ami}", "InvalidAMIID.NotFound", "ec2", "deregister-image", "--image-id", ami):
                    ok = False
                    continue
                for mapping in image.get("BlockDeviceMappings") or []:
                    snapshot = (mapping.get("Ebs") or {}).get("SnapshotId")
                    if snapshot and not delete_aws(f"its snapshot {snapshot}", "InvalidSnapshot.NotFound",
                                                   "ec2", "delete-snapshot", "--snapshot-id", snapshot):
                        ok = False
        elif platform == "azure":
            definition = azure_gallery_image(os_name, version)
            names = az_json("sig", "image-version", "list", "-g", "cf-images", "-r", "cfimages", "-i", definition, "--query", "[].name")
            for name in names:
                if name == keep.rsplit("/", 1)[-1]:
                    continue
                info(f"deleting superseded Azure gallery version {definition}/{name} (can take several minutes)")
                if not az("sig", "image-version", "delete", "-g", "cf-images", "-r", "cfimages", "-i", definition, "-e", name).ok:
                    ok = False
    except Unknown as exc:
        warn(str(exc))
        return False
    return ok


def image_tier_lines() -> list[str]:
    """The kept image tier: CA fingerprint plus every POC image with its CA fingerprint and git SHA."""
    lines = [f"SSH CA: {ca_fingerprint_from_ssm() or 'none'}"]
    try:
        data = aws_json("ec2", "describe-images", "--owners", "self", "--filters", "Name=tag:cf:poc,Values=true")
        for image in data.get("Images") or []:
            tags = {t.get("Key"): t.get("Value") for t in image.get("Tags") or []}
            lines.append("  ".join(str(v) for v in (image.get("ImageId"), image.get("Name"), image.get("CreationDate"),
                                                     tags.get("cf:ssh-ca-fingerprint"), tags.get("cf:git-sha"))))
    except Unknown as exc:
        lines.append(f"AWS images unreadable: {exc}")
    try:
        definitions = az_json("sig", "image-definition", "list", "-g", "cf-images", "-r", "cfimages", "--query", "[].name")
    except Unknown:
        definitions = []
    for definition in definitions:
        try:
            versions = az_json("sig", "image-version", "list", "-g", "cf-images", "-r", "cfimages", "-i", definition)
        except Unknown as exc:
            lines.append(f"{definition}: unreadable ({exc})")
            continue
        for version in versions:
            tags = version.get("tags") or {}
            lines.append("  ".join(str(v) for v in (definition, version.get("name"), version.get("provisioningState"),
                                                     tags.get("cf:ssh-ca-fingerprint"), tags.get("cf:git-sha"))))
    return lines


# ---------------------------------------------------------------- bakes


def watch_bake(run_id: str, stop: threading.Event | None = None) -> int:
    """0 success, 1 completed without success, 2 unknown. Only monitors; never cancels or dispatches."""
    echo(f"bake run: https://github.com/{BUILDER_REPO}/actions/runs/{run_id}")
    deadline = time.monotonic() + BAKE_WATCH_SECONDS
    errors = 0
    while time.monotonic() < deadline:
        result = run(["gh", "run", "view", run_id, "-R", BUILDER_REPO, "--json", "status,conclusion"])
        status, conclusion = "", ""
        if result.ok:
            try:
                data = json.loads(result.out)
                status, conclusion = data.get("status") or "", data.get("conclusion") or ""
            except (ValueError, AttributeError):
                pass
        if isinstance(status, str) and status:
            errors = 0
            echo(f"bake status: {status}" + (f" ({conclusion})" if conclusion else ""))
            if status == "completed":
                if conclusion == "success":
                    echo("bake succeeded")
                    return 0
                if conclusion:
                    warn(f"bake completed with conclusion: {conclusion}")
                    return 1
                # A completed run without a conclusion is not proof of success or failure.
                warn("bake conclusion not available yet; retrying")
        else:
            errors += 1
            warn(f"could not read bake status ({errors}/10 consecutive errors); this does not mean the bake failed")
            if errors >= 10:
                break
        if wait_seconds(30, stop):
            warn(f"monitoring stopped; the bake continues. Resume with: ./poc watch-bake {run_id}")
            return 2
    warn("bake outcome unknown: monitoring stopped after repeated API errors or the two-hour limit")
    warn(f"monitoring did not cancel the workflow; resume with: ./poc watch-bake {run_id}")
    return 2


def run_bake(fp: str, stop: threading.Event | None = None) -> bool:
    # Never retry dispatch: a lost response could still have created a run.
    result = run(["gh", "api", f"repos/{BUILDER_REPO}/actions/workflows/bake.yml/dispatches",
                  "-f", "ref=main", "-F", "return_run_details=true"])
    if not result.ok:
        warn(f"bake dispatch could not be confirmed ({last_line(result.err)}); "
             f"check https://github.com/{BUILDER_REPO}/actions before retrying")
        return False
    try:
        run_id = json.loads(result.out).get("workflow_run_id")
    except (ValueError, AttributeError):
        run_id = None
    if isinstance(run_id, float) and run_id.is_integer():
        run_id = int(run_id)
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        warn(f"dispatch returned no run ID; outcome unknown. Find it at https://github.com/{BUILDER_REPO}/actions; "
             "never assume the latest run is yours")
        return False
    (GEN / "bake-run-id").write_text(f"{run_id}\n")
    return watch_bake(str(run_id), stop) == 0 and verify_bake_images(str(run_id), fp, prune=True, stop=stop)


def _download_manifest(run_id: str, scratch: Path, name: str) -> Path | None:
    for attempt in range(1, 5):
        # gh may skip existing files; a partial download must not contaminate a retry.
        target = Path(tempfile.mkdtemp(dir=scratch, prefix=f"{name}."))
        result = run(["gh", "run", "download", run_id, "-R", BUILDER_REPO, "-n", name, "-D", str(target)])
        if result.ok:
            return target / "packer-manifest.json"
        warn(f"download {name} from bake {run_id}: read failed ({attempt}/4): {last_line(result.err)}")
        if attempt < 4 and pause(2**attempt):
            break
    return None


def _expected_artifact(platform: str, artifact: str) -> str | None:
    if platform == "aws" and re.fullmatch(rf"{re.escape(AWS_REGION)}:ami-[0-9a-f]+", artifact):
        return artifact.split(":", 1)[1]
    azure = (rf"/subscriptions/{re.escape(AZURE_SUBSCRIPTION)}/resourceGroups/cf-images/providers/Microsoft\.Compute"
             r"/galleries/cfimages/images/[A-Za-z0-9._-]+/versions/[0-9]+\.[0-9]+\.[0-9]+")
    if platform == "azure" and re.fullmatch(azure, artifact):
        return artifact
    return None


def verify_bake_images(run_id: str, fp: str, prune: bool = True, stop: threading.Event | None = None) -> bool:
    """Every images.yaml row of the run's commit must have produced exactly one artifact that is now the
    current usable image of its row and trusts CA fp. Only prune after every row is verified."""
    result = gh_read("api", f"repos/{BUILDER_REPO}/actions/runs/{run_id}")
    if not result.ok:
        warn(f"could not read bake run {run_id}")
        return False
    try:
        data = json.loads(result.out)
    except ValueError:
        data = {}
    sha = data.get("head_sha") if isinstance(data, dict) else None
    confirmed = isinstance(data, dict) and (
        data.get("path") == ".github/workflows/bake.yml"
        and (data.get("head_repository") or {}).get("full_name") == BUILDER_REPO
        and data.get("head_branch") == "main" and data.get("event") == "workflow_dispatch"
        and data.get("status") == "completed" and data.get("conclusion") == "success"
    )
    if not confirmed or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        warn(f"run {run_id} is not a confirmed successful bake.yml dispatch on {BUILDER_REPO} main")
        return False
    try:
        rows = image_rows(sha)
    except Unknown as exc:
        warn(f"could not read images.yaml at builder {sha}: {exc}")
        return False
    scratch = Path(tempfile.mkdtemp(dir=GEN, prefix="bake."))
    verified = []
    try:
        for os_name, version, platform in rows:
            name = f"manifest-{os_name}-{version}-{platform}"
            builder = BUILDERS.get(platform)
            if builder is None:
                warn(f"./poc cannot verify images for platform {platform}")
                return False
            manifest = _download_manifest(run_id, scratch, name)
            if manifest is None:
                warn(f"could not download {name} from bake {run_id}")
                return False
            try:
                builds = json.loads(manifest.read_text())["builds"]
                artifacts = [b.get("artifact_id") for b in builds if b.get("builder_type") == builder]
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                artifacts = []
            if len(artifacts) != 1 or not isinstance(artifacts[0], str):
                warn(f"bake run {run_id} has no single {builder} artifact in {name}")
                return False
            expected = _expected_artifact(platform, artifacts[0])
            if expected is None:
                warn(f"unexpected {platform} artifact in {name}: {artifacts[0]}")
                return False
            deadline = time.monotonic() + 120
            while True:
                try:
                    found = current_image(platform, os_name, version, fp)
                except Unknown:
                    found = None
                if found and found[0].lower() == expected.lower():
                    break
                if time.monotonic() >= deadline:
                    warn(f"bake {run_id}: {platform} {os_name} {version} artifact {expected} is not the current usable image trusting CA {fp}")
                    return False
                if wait_seconds(5, stop):
                    return False
            echo(f"Bake {run_id} {platform} {os_name} {version}: {expected}")
            verified.append((platform, os_name, version, expected))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if prune:
        for platform, os_name, version, expected in verified:
            if not prune_superseded(platform, os_name, version, expected):
                warn(f"could not remove every superseded {platform} {os_name} {version} image; the next bake or down retries")
    return True
