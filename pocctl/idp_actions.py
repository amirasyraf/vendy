"""Build and identify the exact IDP image published by the trusted main workflow."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import NamedTuple

from .core import GEN, OWNER, die, info, last_line, require, run, wait_seconds, warn

REPO = f"{OWNER}/vendy-idp"
WORKFLOW = "build-idp.yml"
COMMIT = re.compile(r"[0-9a-f]{40}")
RUN_ID = re.compile(r"[1-9][0-9]*")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
IMAGE = re.compile(rf"ghcr\.io/{OWNER}/vendy-idp@{IMAGE_ID.pattern}")
CHECKPOINT = GEN / "gha-bootstrap.json"


class Build(NamedTuple):
    commit: str
    run_id: str


class Artifact(NamedTuple):
    reference: str
    image_id: str


def _api(*args: str) -> str:
    return require(run(["gh", "api", *args], timeout=30), "IDP Actions API request failed")


def _build(commit: object, run_id: object) -> Build:
    if isinstance(commit, str) and COMMIT.fullmatch(commit) and RUN_ID.fullmatch(str(run_id)):
        return Build(commit, str(run_id))
    die(f"Invalid IDP build identifier. Inspect https://github.com/{REPO}/actions/workflows/{WORKFLOW} before retrying.")


def select_build(existing: str | None = None) -> Build:
    if existing:
        record = json.loads(_api(f"repos/{REPO}/actions/runs/{existing}"))
        build = _build(record.get("head_sha"), existing)
        _check_run(record, build)
        return build
    commit = _api(f"repos/{REPO}/commits/main", "--jq", ".sha").strip()
    if not COMMIT.fullmatch(commit):
        die("could not pin the IDP main commit")
    endpoint = f"repos/{REPO}/actions/workflows/{WORKFLOW}"
    # Include active runs so interruption never needs to launch another build for the same commit.
    records = json.loads(_api(
        "-X", "GET", f"{endpoint}/runs", "-f", f"head_sha={commit}", "-f", "branch=main",
        "-f", "event=workflow_dispatch", "-F", "per_page=30",
    ))["workflow_runs"]
    for record in sorted(records, key=lambda row: row.get("conclusion") != "success"):
        if record.get("status") != "completed" or record.get("conclusion") == "success":
            build = _build(commit, record.get("id"))
            _check_run(record, build)
            # Very old builds may have lost the metadata artifact.
            if record.get("conclusion") == "success":
                name = f"idp-image-{record.get('run_attempt')}"
                artifacts = json.loads(_api(f"repos/{REPO}/actions/runs/{build.run_id}/artifacts", "-X", "GET", "-f", f"name={name}"))
                if not any(item.get("name") == name and item.get("expired") is False for item in artifacts["artifacts"]):
                    continue
            info(f"reusing IDP build: https://github.com/{REPO}/actions/runs/{build.run_id}")
            return build
    response = run(["gh", "api", f"{endpoint}/dispatches", "-f", "ref=main", "-f", f"inputs[commit]={commit}",
                    "-F", "return_run_details=true"], timeout=30)
    if not response.ok:
        die(f"IDP dispatch could not be confirmed: {last_line(response.err)}. Check Actions before retrying.")
    try:
        build = _build(commit, json.loads(response.out).get("workflow_run_id"))
    except (ValueError, AttributeError):
        die(f"IDP dispatch outcome unknown. Check https://github.com/{REPO}/actions before retrying.")
    info(f"IDP build: https://github.com/{REPO}/actions/runs/{build.run_id}")
    return build


def check_pull_credentials() -> None:
    user, token = os.environ.get("GHCR_PULL_USER", ""), os.environ.get("GHCR_PULL_TOKEN", "")
    if (user or token) and (not re.fullmatch(r"[a-zA-Z0-9-]+", user) or not token):
        die("set both GHCR_PULL_USER and GHCR_PULL_TOKEN for a private package, or omit both for a public package")


def _check_run(record: dict, build: Build) -> None:
    if (str(record.get("id")) != build.run_id
            or record.get("path") != f".github/workflows/{WORKFLOW}"
            or record.get("event") != "workflow_dispatch"
            or record.get("head_branch") != "main"
            or record.get("head_sha") != build.commit
            or record.get("head_repository", {}).get("full_name") != REPO):
        die(f"IDP run {build.run_id} does not match the trusted workflow and commit")


def wait_for_artifact(build: Build, stop: threading.Event | None = None) -> Artifact | None:
    deadline, errors = time.monotonic() + 35 * 60, 0
    while time.monotonic() < deadline and not (stop and stop.is_set()):
        result = run(["gh", "api", f"repos/{REPO}/actions/runs/{build.run_id}"], timeout=30)
        if result.ok:
            errors = 0
            record = json.loads(result.out)
            _check_run(record, build)
            if record["status"] == "completed":
                if record["conclusion"] != "success":
                    die(f"IDP build {build.run_id}: {record['conclusion']}. Inspect gh run view --log-failed "
                        f"-R {REPO} {build.run_id}.")
                return _download(build, str(record["run_attempt"]))
        else:
            errors += 1
            warn(f"IDP run {build.run_id}: {last_line(result.err)}")
            if errors >= 5:
                break
        if wait_seconds(10, stop):
            return None
    if stop and stop.is_set():
        return None
    die(f"IDP build {build.run_id} outcome unknown; the run was not cancelled")


def _download(build: Build, attempt: str) -> Artifact:
    with tempfile.TemporaryDirectory(dir=GEN, prefix="idp-artifact.") as scratch:
        require(run(["gh", "run", "download", build.run_id, "-R", REPO, "-n", f"idp-image-{attempt}", "-D", scratch],
                    timeout=60), f"could not download IDP metadata from run {build.run_id}")
        try:
            data = json.loads((Path(scratch) / "idp-image.json").read_text())
            ref, image_id = data["image"], data["image_id"]
            if (data["commit"] == build.commit and data["run_id"] == build.run_id and data["attempt"] == attempt
                    and IMAGE.fullmatch(ref) and IMAGE_ID.fullmatch(image_id)):
                return Artifact(ref, image_id)
        except (OSError, KeyError, TypeError, ValueError):
            pass
    die(f"IDP run {build.run_id} has invalid artifact metadata")


def clone_deployer(build: Build, directory: Path) -> None:
    require(run(["git", "init", "-q", str(directory)]), "could not initialize the IDP deploy checkout")
    require(run(["git", "fetch", "-q", "--depth", "1", f"git@github.com:{REPO}.git", build.commit], cwd=directory,
                timeout=120), "could not fetch the pinned IDP source")
    require(run(["git", "checkout", "-q", "--detach", build.commit], cwd=directory), "could not check out the IDP deployer")


def save_checkpoint(build: Build, instance: str, reused: bool) -> None:
    temp = CHECKPOINT.with_suffix(".tmp")
    temp.write_text(json.dumps({"commit": build.commit, "run_id": build.run_id, "instance": instance, "reused": reused}))
    temp.replace(CHECKPOINT)


def load_checkpoint(instance: str | None) -> tuple[Build, bool]:
    try:
        data = json.loads(CHECKPOINT.read_text())
        if instance and data["instance"] == instance and isinstance(data["reused"], bool):
            return _build(data["commit"], data["run_id"]), data["reused"]
    except (OSError, KeyError, TypeError, ValueError):
        pass
    die("no GitHub Actions checkpoint matches the existing IDP instance")
