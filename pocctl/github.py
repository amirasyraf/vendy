"""GitHub state the lifecycle owns: maintenance gates, resource workflows, wiring and VM inventory."""

from __future__ import annotations

import functools
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from .core import (
    AWS_ACCOUNT, AWS_REGION, AZURE_LOCATION, AZURE_SUBSCRIPTION, AZURE_TENANT, MAINTENANCE_VARIABLE, OWNER,
    PLATFORM_REPO, PROJECTS, RUN_CANCEL_TIMEOUT, WORKFLOWS, Unknown, _err, _write, die, gh_read, info, last_line,
    parallel, pause, run, warn,
)

WIRING = ("TF_STATE_BUCKET", "BACKSTAGE_URL", "BACKSTAGE_TOKEN")
REPO_VARIABLES = ("AWS_ROLE_ARN", "AWS_REGION", "AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID", "AZURE_LOCATION")
SETTLE_SECONDS = 90

T = TypeVar("T")


def settle(read: Callable[[], T], done: Callable[[T], bool]) -> T | None:
    """GitHub reads can trail its own writes (a read right after a successful write may return the old
    value, and later reads can still hit a lagging replica). Every check of state that this command or a
    recent write changed polls until done(value) or SETTLE_SECONDS pass. Returns the last value read,
    or None when it was unreadable. A real mismatch still fails, just after the window."""
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        try:
            value: T | None = read()
        except Unknown:
            value = None
        if (value is not None and done(value)) or time.monotonic() >= deadline or pause(3):
            return value


def _gate(repo: str) -> str:
    result = gh_read("variable", "get", MAINTENANCE_VARIABLE, "-R", repo)
    if not result.ok:
        raise Unknown(f"could not read {MAINTENANCE_VARIABLE} on {repo}")
    return result.out.strip()


def set_maintenance_gate(value: str, *repos: str) -> None:
    for repo in repos:
        result = run(["gh", "variable", "set", MAINTENANCE_VARIABLE, "-R", repo, "-b", value])
        if not result.ok:
            warn(f"setting {MAINTENANCE_VARIABLE}={value} on {repo} reported an error ({last_line(result.err)}); checking current value")
        if not verify_maintenance_gate(value, repo):
            die(f"could not confirm {MAINTENANCE_VARIABLE}={value} on {repo}; refusing to continue")


def verify_maintenance_gate(expected: str, *repos: str) -> bool:
    for repo in repos:
        value = settle(functools.partial(_gate, repo), lambda v: v == expected)
        if value is None:
            warn(f"could not read {MAINTENANCE_VARIABLE} on {repo}")
            return False
        if value != expected:
            warn(f"{MAINTENANCE_VARIABLE} on {repo} is '{value}', expected {expected}")
            return False
    return True


def check_repo_variables(*repos: str) -> None:
    pairs = [(repo, var) for repo in repos for var in REPO_VARIABLES]
    results = parallel(lambda pair: gh_read("variable", "get", pair[1], "-R", pair[0]), pairs)
    for (repo, var), result in zip(pairs, results):
        if not result.ok:
            die(f"could not read {var} on {repo} (DESIGN.md section 9)")
        value = result.out.strip()
        if not value:
            die(f"{repo} is missing {var} (DESIGN.md section 9)")
        expected = {
            "AWS_ROLE_ARN": (value.startswith(f"arn:aws:iam::{AWS_ACCOUNT}:role/"), "AWS role"),
            "AWS_REGION": (value == AWS_REGION, "AWS region"),
            "AZURE_TENANT_ID": (value == AZURE_TENANT, "Azure tenant"),
            "AZURE_SUBSCRIPTION_ID": (value == AZURE_SUBSCRIPTION, "Azure subscription"),
            "AZURE_LOCATION": (value == AZURE_LOCATION, "Azure location"),
        }.get(var)
        if expected and not expected[0]:
            die(f"{repo} uses an unexpected {expected[1]}")


def _workflow_state(repo: str, workflow: str) -> str:
    result = gh_read("api", f"repos/{repo}/actions/workflows/{workflow}", "--jq", ".state")
    if not result.ok:
        raise Unknown(f"could not inspect {repo}/{workflow}")
    return result.out.strip()


def workflow_action(action: str, specs: tuple[tuple[str, str], ...] | list[tuple[str, str]]) -> None:
    """Always write, then wait for the state to settle: deciding to skip from a read that may be stale
    could leave a workflow in the wrong state. Enabling an active workflow succeeds; disabling an
    inactive one returns HTTP 403 "not active", which the settled state read then confirms."""
    expected = "active" if action == "enable" else "disabled_manually"
    for repo, workflow in specs:
        result = run(["gh", "api", "-X", "PUT", f"repos/{repo}/actions/workflows/{workflow}/{action}"])
        if not result.ok and "not active" not in result.err:
            warn(f"{action} {repo}/{workflow} reported an error ({last_line(result.err)}); checking current state")
        state = settle(functools.partial(_workflow_state, repo, workflow), lambda s: s == expected)
        if state != expected:
            die(f"could not confirm {action} of {repo}/{workflow} after {SETTLE_SECONDS}s (state: {state or 'unreadable'})")
        info(f"{repo}/{workflow}: {state}")


def verify_workflows_disabled() -> bool:
    for repo, workflow in WORKFLOWS:
        state = settle(functools.partial(_workflow_state, repo, workflow), lambda s: s == "disabled_manually")
        if state is None:
            warn(f"could not inspect {repo}/{workflow}")
            return False
        if state != "disabled_manually":
            warn(f"{repo}/{workflow} is not manually disabled (state: {state})")
            return False
    return True


def verify_frozen() -> bool:
    return verify_maintenance_gate("1", *(repo for repo, _ in WORKFLOWS)) and verify_workflows_disabled()


def list_active_runs(repo: str, workflow: str) -> list[str]:
    result = gh_read("api", "--paginate", f"repos/{repo}/actions/workflows/{workflow}/runs?per_page=100",
                     "--jq", '.workflow_runs[] | select(.status != "completed") | .id')
    if not result.ok:
        raise Unknown(f"could not list {repo}/{workflow} runs")
    return result.out.split()


def active_runs() -> list[str]:
    """Non-completed runs of both resource workflows as "<repo>:<run-id>"; Unknown if unreadable."""
    return [f"{repo}:{run_id}" for repo, workflow in WORKFLOWS for run_id in list_active_runs(repo, workflow)]


def cancel_runs() -> None:
    deadline = time.monotonic() + RUN_CANCEL_TIMEOUT
    empty = 0
    active: list[str] = []
    while time.monotonic() < deadline:
        try:
            active = active_runs()
        except Unknown:
            die("could not verify workflow quiescence; refusing to continue")
        if not active:
            empty += 1
            if empty == 2:
                return
        else:
            empty = 0
            for item in active:
                repo, _, run_id = item.rpartition(":")
                if not re.fullmatch(r"[1-9][0-9]*", run_id):
                    die("unexpected workflow run ID; refusing to continue")
                if run(["gh", "run", "cancel", run_id, "-R", repo]).ok:
                    info(f"requested cancellation of {repo} run {run_id}")
                else:
                    warn(f"{repo} run {run_id} is not cancellable yet or already finishing")
        if pause(10):
            die("interrupted while cancelling resource workflow runs")
    _write(_err(), "".join(f"    {item}\n" for item in active))
    die(f"resource-creating workflows are still active after {RUN_CANCEL_TIMEOUT}s; refusing to sweep")


def verify_quiescent() -> bool:
    if not verify_frozen():
        return False
    # Runs cancelled moments ago can still be listed as in progress.
    active = settle(active_runs, lambda runs: not runs)
    if active is None:
        warn("could not verify workflow quiescence")
        return False
    if active:
        warn(f"active resource workflow runs: {' '.join(active)}")
        return False
    return True


INVENTORY_DIRS = ("vms", "containers")


def verify_inventory_empty() -> str:
    """"empty", "nonempty" (confirmed) or "unknown" for platform main's vms/ and containers/."""
    states = [_directory_empty(directory) for directory in INVENTORY_DIRS]
    if "nonempty" in states:
        return "nonempty"
    return "empty" if all(state == "empty" for state in states) else "unknown"


def _directory_empty(directory: str) -> str:
    result = gh_read("api", f"repos/{PLATFORM_REPO}/contents/{directory}?ref=main")
    if not result.ok:
        return "unknown"
    try:
        items = json.loads(result.out)
    except ValueError:
        return "unknown"
    if not isinstance(items, list) or not all(
        isinstance(i, dict) and isinstance(i.get("name"), str) and isinstance(i.get("type"), str) for i in items
    ):
        return "unknown"
    return "empty" if all(i["name"] == ".gitkeep" and i["type"] == "file" for i in items) else "nonempty"


def reset_inventory(checkout: Path) -> bool:
    """Only down may clear the inventory, after the cloud and state checks have passed."""

    def git(*args: str) -> bool:
        result = run(["git", *args], cwd=checkout)
        if not result.ok:
            warn(f"git {args[0]} in {checkout}: {last_line(result.err)}")
        return result.ok

    for directory in INVENTORY_DIRS:
        if not git("rm", "-rq", "--ignore-unmatch", directory):
            return False
        (checkout / directory).mkdir(exist_ok=True)
        (checkout / directory / ".gitkeep").touch()
        if not git("add", f"{directory}/.gitkeep"):
            return False
    staged = run(["git", "diff", "--cached", "--quiet"], cwd=checkout)
    if staged.rc > 1:
        return False
    if staged.rc == 1:
        if not git("commit", "-qm", "poc: reset VM and container inventory") or not git("push", "-q", "origin", "HEAD:main"):
            return False
        info("inventory reset")
    inventory = settle(verify_inventory_empty, lambda state: state == "empty")
    if inventory != "empty":
        warn(f"platform main's VM and container inventory is {inventory or 'unreadable'} after the reset")
        return False
    return True


def platform_wiring() -> list[str]:
    names: list[str] = []
    for kind in ("variable", "secret"):
        result = gh_read(kind, "list", "-R", PLATFORM_REPO, "--json", "name")
        if not result.ok:
            raise Unknown(f"could not list platform {kind}s")
        try:
            entries = json.loads(result.out)
            names += [e["name"] for e in entries if e["name"] in WIRING]
        except (ValueError, TypeError, KeyError):
            raise Unknown(f"unexpected platform {kind} list") from None
    return names


def clear_platform_wiring() -> bool:
    for kind in ("variable", "secret"):
        result = gh_read(kind, "list", "-R", PLATFORM_REPO, "--json", "name")
        if not result.ok:
            return False
        try:
            present = {e["name"] for e in json.loads(result.out)}
        except (ValueError, TypeError, KeyError):
            return False
        for name in WIRING:
            if name in present and not run(["gh", kind, "delete", name, "-R", PLATFORM_REPO]).ok:
                warn(f"could not delete platform {kind} {name}; confirming current state")
    return verify_wiring_cleared()


def verify_wiring_cleared() -> bool:
    names = settle(platform_wiring, lambda found: not found)
    if names is None:
        warn("could not read platform GitHub wiring")
        return False
    if names:
        warn(f"platform GitHub wiring remains: {' '.join(names)}")
        return False
    return True


def ensure_repos() -> None:
    for repo in ("vendy-idp", "vendy-builder", "vendy-platform"):
        if not (PROJECTS / repo / ".git").exists():
            result = run(["git", "clone", "-q", f"git@github.com:{OWNER}/{repo}.git", str(PROJECTS / repo)])
            if not result.ok:
                die(f"could not clone {repo}: {last_line(result.err)}")
