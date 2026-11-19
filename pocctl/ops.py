"""Read-only inspection commands and local password management."""

from __future__ import annotations

import getpass
import json
import os
import re
from pathlib import Path

from .cloud import state_bucket_keys
from .core import (
    BUILDER_REPO, HEALTH_URL, IDP_DIR, INFRA_DIR, PASSWORDS_FILE, PLATFORM_REPO, USERS_FILE, Unknown,
    aws_json, die, info, infra_out, log, require_tools, root_state, run, spawn, ssh,
)
from .idp import idp_host
from .images import image_tier_lines

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def cmd_status() -> None:
    """A read-only snapshot. The Actions list is best-effort; no output does not prove quiescence."""
    require_tools()
    try:
        state = root_state(INFRA_DIR)
    except Unknown:
        die("cannot read local Terraform state")

    def shared_infra() -> list[str]:
        if not state:
            return ["none"]
        code = run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10", HEALTH_URL]).out.strip()
        return [
            f"IDP host:     {infra_out('idp_ip')} ({infra_out('idp_instance_id')})",
            f"State bucket: {infra_out('state_bucket')}",
            f"IDP health:   {'unreachable' if code in ('', '000') else f'HTTP {code}'}",
        ]

    def instances() -> list[str]:
        try:
            rows = aws_json("ec2", "describe-instances", "--filters", "Name=tag:cf:poc,Values=true",
                            "Name=instance-state-name,Values=pending,running,stopping,stopped", "--query",
                            "Reservations[].Instances[].[InstanceId,State.Name,PublicIpAddress,Tags[?Key==`Name`]|[0].Value]")
        except Unknown as exc:
            return [f"unreadable: {exc}"]
        return ["  ".join(str(v) for v in row) for row in rows]

    def azure_groups() -> list[str]:
        result = run(["az", "group", "list", "--query", "[?tags.\"cf:poc\"=='true'].[name,properties.provisioningState]", "-o", "json"])
        if not result.ok:
            return ["unreadable"]
        return ["  ".join(str(v) for v in row) for row in json.loads(result.out)]

    def vm_states() -> list[str]:
        try:
            keys = state_bucket_keys(require_present=True)
        except Unknown:
            return ["WARN: could not verify the state bucket or list its contents"]
        if keys is None:
            return ["no owned state bucket"]
        return keys or ["state bucket is empty"]

    def pipeline_runs() -> list[str]:
        lines = []
        for repo in (PLATFORM_REPO, BUILDER_REPO):
            result = run(["gh", "run", "list", "-R", repo, "-L", "20", "--json", "databaseId,status,displayTitle"])
            if result.ok:
                lines += [f"{r['databaseId']} {r['status']} {r['displayTitle']}" for r in json.loads(result.out)
                          if r.get("status") != "completed"]
        return lines

    sections = [
        ("Shared infra", shared_infra),
        ("Image tier: SSH CA and golden images (id, name/state, created, CA fingerprint, git SHA)", image_tier_lines),
        ("Instances tagged cf:poc=true", instances),
        ("Azure resource groups tagged cf:poc=true", azure_groups),
        ("VM state files", vm_states),
        ("Active pipeline runs", pipeline_runs),
    ]
    futures = [spawn(fn) for _, fn in sections]
    for (title, _), future in zip(sections, futures):
        log(title)
        for line in future.result():
            info(line)


def cmd_logs(args: list[str]) -> int:
    """Read-only. Pipe to grep/jq locally, e.g. ./poc logs caddy 10m | grep /events"""
    service = args[0] if args else "backstage"
    since = args[1] if len(args) > 1 else "30m"
    if service not in ("backstage", "caddy", "postgres", "all"):
        die("service must be backstage, caddy, postgres or all")
    if not re.fullmatch(r"[0-9]+[smh]", since):
        die("since must look like 30m, 2h or 90s")
    target = "" if service == "all" else service
    return ssh(idp_host(), f"cd /opt/idp && docker compose logs --no-color --no-log-prefix --since {since} {target}",
               capture=False).rc


def cmd_task(args: list[str]) -> int:
    """Read-only. If the task page is blank but events exist here, the problem is delivery to the browser."""
    task = args[0] if args else ""
    if not UUID.fullmatch(task):
        die("usage: ./poc task <task-uuid>")
    remote = (
        "cd /opt/idp && docker compose exec -T postgres psql -U backstage -d backstage_plugin_scaffolder "
        f"-c \"select status, created_at, last_heartbeat_at, created_by from tasks where id = '{task}'\" "
        f"-c \"select id, event_type, created_at, left(body::text, 300) as body from task_events where task_id = '{task}' order by id\""
    )
    return ssh(idp_host(), remote, capture=False).rc


def cmd_run(args: list[str]) -> int:
    """Read-only. Orchestrator state of one VM run (the IDP run page shows the same data)."""
    run_id = args[0] if args else ""
    if not re.fullmatch(r"[1-9][0-9]{0,9}", run_id):
        die("usage: ./poc run <run-id>")
    remote = (
        "cd /opt/idp && docker compose exec -T postgres psql -U backstage -d backstage_plugin_vms "
        "-c \"select id, kind, name, team, status, requested_by, to_timestamp(created_at/1000) as created, "
        f"left(error, 200) as error from vm_runs where id = {run_id}\" "
        "-c \"select stage, status, attempt, max_attempts + budget_start as max, gha_run_id, conclusion, "
        "to_timestamp(next_attempt_at/1000) as next_attempt, left(error, 160) as error from vm_run_stages "
        f"where run_id = {run_id} order by position\""
    )
    return ssh(idp_host(), remote, capture=False).rc


def _set_entry(path: Path, user: str, value: str) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if not line.startswith(f"{user}:")] + [f"{user}:{value}"]
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_TRUNC | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(lines) + "\n")
    os.replace(temp, path)


def cmd_set_password(args: list[str]) -> None:
    """Add or change an IDP login. The user also needs an entry in the idp repo's catalog/org.yaml.
    Takes effect on the next deploy (./poc up or ./poc deploy-idp)."""
    user = args[0] if len(args) == 1 else ""
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", user):
        die("usage: ./poc set-password <username>")
    helper = IDP_DIR / "deploy/hash-password.mjs"
    if not helper.is_file():
        die(f"missing {helper}")
    password = getpass.getpass(f"Password for {user}: ")
    if getpass.getpass("Repeat: ") != password:
        die("passwords don't match")
    if len(password) < 8:
        die("use at least 8 characters")
    result = run(["mise", "exec", "--", "node", "deploy/hash-password.mjs"], input=password, cwd=IDP_DIR)
    hashed = result.out.strip()
    if not result.ok or not hashed or "\n" in hashed:
        die("hashing failed")
    os.umask(0o077)
    _set_entry(USERS_FILE, user, hashed)
    _set_entry(PASSWORDS_FILE, user, password)
    info(f"password set for {user} (plaintext kept in .poc-passwords); deploy with ./poc deploy-idp")

