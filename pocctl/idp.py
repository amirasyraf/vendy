"""The IDP host: readiness, DNS, TLS storage, deploys and health checks."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .core import (
    AWS_ACCOUNT, AWS_REGION, AZURE_READER_CLIENT_ID, AZURE_SUBSCRIPTION, AZURE_TENANT, CADDY_BACKUP, CADDY_VOLUME, CF_DNS_DIR, DOMAIN, GEN, HEALTH_URL, IDP_DIR, INFRA_DIR, LOGS,
    POC_ENV, USERS_FILE, ProcessGroup, Unknown, aws, aws_json, die, echo, info, nonempty, pause, poc_env_github_token,
    run, show_tail, spawn, ssh, tf, tf_output, tls_backup_has_cert, warn,
)

IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
IPV4 = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+")


def wait_for_ssh_ready(ip: str) -> bool:
    deadline = time.monotonic() + 600
    while True:
        if ssh(ip, "test -f /var/lib/cloud/instance/cf-idp-ready").ok:
            return True
        if time.monotonic() >= deadline:
            warn(f"IDP host {ip} not ready after 10 min")
            return False
        if pause(10):
            return False


def dns_apply(ip: str) -> bool:
    """Point the targeted Cloudflare record at ip, or remove it when ip is empty."""
    path = LOGS / "dns.log"
    ok = tf(CF_DNS_DIR, "apply", "-auto-approve", "-input=false", "-target=cloudflare_dns_record.idp",
            "-target=cloudflare_dns_record.idp_apps", "-var", f"idp_ip={ip}", log=path).ok
    if not ok:
        show_tail(path)
    return ok


def write_idp_env(path: Path) -> bool:
    token = poc_env_github_token()
    users = base64.b64encode(USERS_FILE.read_bytes()).decode() if nonempty(USERS_FILE) else ""
    outputs = {name: tf_output(INFRA_DIR, name) for name in
               ("postgres_password", "backstage_token", "ssh_ca_secret_id", "azure_reader_secret_id", "idp_ip",
                "k3s_version", "cluster_autoscaler_version", "k3s_token", "aws_worker_groups", "azure_workers_rg",
                "azure_worker_scale_sets")}
    if not token or not users or not all(outputs.values()):
        warn("IDP configuration has missing required values")
        return False
    content = (
        f"IDP_DOMAIN={DOMAIN}\nIDP_USERS_B64={users}\nGITHUB_TOKEN={token}\n"
        f"POSTGRES_PASSWORD={outputs['postgres_password']}\nBACKSTAGE_TOKEN={outputs['backstage_token']}\n"
        f"AWS_REGION={AWS_REGION}\nSSH_CA_SECRET_ID={outputs['ssh_ca_secret_id']}\n"
        f"AZURE_READER_SECRET_ID={outputs['azure_reader_secret_id']}\n"
        f"K3S_VERSION={outputs['k3s_version']}\nCA_VERSION={outputs['cluster_autoscaler_version']}\n"
        f"K3S_TOKEN={outputs['k3s_token']}\nIDP_PUBLIC_IP={outputs['idp_ip']}\n"
        f"AWS_WORKER_GROUPS={outputs['aws_worker_groups']}\n"
        f"AZURE_SUBSCRIPTION_ID={AZURE_SUBSCRIPTION}\nAZURE_TENANT_ID={AZURE_TENANT}\n"
        f"AZURE_WORKERS_RG={outputs['azure_workers_rg']}\nAZURE_WORKER_SCALE_SETS={outputs['azure_worker_scale_sets']}\n"
        f"AZURE_AUTOSCALER_CLIENT_ID={AZURE_READER_CLIENT_ID}\n"
    )
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
    return True


def _pinned_curl(ip: str, *args: str, token: str | None = None) -> list[str]:
    # Pin the new host: a resolver may still cache the NXDOMAIN from the pre-bootstrap DNS check.
    # The token is read from stdin (-H @-) so it never appears in the process list.
    return ["curl", "--resolve", f"{DOMAIN}:443:{ip}", *(("-H", "@-") if token else ()), *args]


def health_code(ip: str, timeout: int = 5) -> str:
    result = run(_pinned_curl(ip, "-fsS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(timeout), HEALTH_URL))
    return result.out.strip() if result.ok else ""


def refresh_catalog_locations(ip: str) -> bool:
    """The catalog keeps entities in Postgres across redeploys; re-read every location now so changes
    to org.yaml (new users/teams) take effect immediately instead of on the next processing cycle."""
    token = tf_output(INFRA_DIR, "backstage_token")
    if not token:
        return False
    header = f"Authorization: Bearer {token}\n"
    result = run(_pinned_curl(ip, "-fsS", "--max-time", "10", f"https://{DOMAIN}/api/catalog/entities?filter=kind=location",
                              token=token), input=header)
    try:
        entities = json.loads(result.out) if result.ok else None
    except ValueError:
        entities = None
    if not isinstance(entities, list) or not entities:
        return False
    for entity in entities:
        metadata = entity.get("metadata") or {}
        ref = f"location:{metadata.get('namespace') or 'default'}/{metadata.get('name')}"
        payload = json.dumps({"entityRef": ref})
        posted = run(_pinned_curl(ip, "-fsS", "--max-time", "10", "-o", "/dev/null", "-X", "POST",
                                  "-H", "Content-Type: application/json", "-d", payload,
                                  f"https://{DOMAIN}/api/catalog/refresh", token=token), input=header)
        if not posted.ok:
            return False
    echo("catalog refresh requested")
    return True


def save_caddy_data(ip: str) -> bool:
    """Caddy's TLS storage is kept on the laptop across cycles, so `up` doesn't hit Let's Encrypt's
    duplicate-certificate limit (5/week). Compose project "idp" (dir /opt/idp)."""
    if not ip:
        return True
    fd, name = tempfile.mkstemp(dir=GEN, prefix="caddy-data.")
    temp = Path(name)
    with os.fdopen(fd, "wb") as stream:
        # Caddy is already on the host; avoid pulling a Docker Hub image during teardown.
        result = ssh(ip, "cd /opt/idp && docker compose exec -T caddy tar cz -C /data .", stdout=stream)
    if result.ok and tls_backup_has_cert(temp):
        os.replace(temp, CADDY_BACKUP)
        info(f"saved TLS cert storage to {CADDY_BACKUP}")
    else:
        temp.unlink(missing_ok=True)
        info("no TLS cert to save (keeping any previous backup)")
    return True


def restore_caddy_data(ip: str) -> bool:
    if not tls_backup_has_cert(CADDY_BACKUP):
        echo("no valid saved TLS cert; Caddy will request one")
        return True
    remote = (
        "running=$(docker ps -q --filter label=com.docker.compose.project=idp "
        "--filter label=com.docker.compose.service=caddy) "
        "&& if [ -n \"$running\" ]; then echo 'Caddy is already running; keeping its TLS storage'; else docker volume create "
        f"--label com.docker.compose.project=idp --label com.docker.compose.volume=caddy_data {CADDY_VOLUME} >/dev/null "
        f"&& docker run --rm -i -v {CADDY_VOLUME}:/data alpine tar xz -C /data "
        "&& echo 'restored saved TLS cert storage'; fi"
    )
    with open(CADDY_BACKUP, "rb") as stream:
        return ssh(ip, remote, stdin=stream, capture=False).ok


def _build_image(project_dir: Path, group: ProcessGroup) -> str | None:
    """Build the IDP artifact with the checkout's build.sh; progress goes to idp-build.log."""
    with open(LOGS / "idp-build.log", "w") as log_file:
        result = run(["bash", str(project_dir / "deploy/build.sh")], stderr=log_file, group=group)
    lines = result.out.strip().splitlines()
    image = lines[-1].strip() if result.ok and lines else ""
    return image if IMAGE_ID.fullmatch(image) else None


def deploy_idp(ip: str, mode: str, image: str | None = None, project_dir: Path = IDP_DIR) -> bool:
    """Deploy to the host and wait until it is healthy. Bootstrap also waits for host initialization,
    publishes DNS and restores TLS storage, while the image builds locally in parallel."""
    fd, name = tempfile.mkstemp(dir=GEN, prefix="idp.env.")
    os.close(fd)
    env_file = Path(name)
    build = None
    build_group = ProcessGroup()
    try:
        if mode == "bootstrap":
            if image is None:
                echo(f"building the IDP image while the host initializes; log: {LOGS / 'idp-build.log'}")
                build = spawn(lambda: _build_image(project_dir, build_group))
            if not wait_for_ssh_ready(ip):
                return False
            echo("host ready")
            if not dns_apply(ip):
                return False
            echo(f"dns: {DOMAIN} -> {ip}")
            if not restore_caddy_data(ip):
                return False
            if build is not None:
                echo("waiting for the IDP image build")
                image = build.result()
                if image is None:
                    show_tail(LOGS / "idp-build.log", 40)
                    warn(f"IDP image build failed (see {LOGS / 'idp-build.log'})")
                    return False
                echo(f"Built artifact {image}")
        start = time.monotonic()
        if not write_idp_env(env_file):
            return False
        echo(f"Prepare IDP configuration: {int(time.monotonic() - start)}s")
        command = [str(project_dir / "deploy/deploy.sh"), ip, str(env_file), *([image] if image else [])]
        if not run(command, capture=False).ok:
            return False
        start = time.monotonic()
        echo(f"waiting for {HEALTH_URL}")
        while time.monotonic() < start + 600:
            if health_code(ip) == "200" and refresh_catalog_locations(ip):
                echo(f"IDP healthy; readiness and catalog reachable after {int(time.monotonic() - start)}s")
                return True
            if pause(2):
                return False
        warn("IDP readiness or authenticated catalog check failed after 10 min")
        return False
    finally:
        env_file.unlink(missing_ok=True)
        if build is not None:
            # Stop the whole build (yarn, docker) and wait for it before the caller removes the checkout.
            if not build.done():
                build_group.cancel()  # also refuses a build that has not started yet
            with contextlib.suppress(Exception):
                build.result()


def existing_idp_ip() -> str:
    ip, instance = tf_output(INFRA_DIR, "idp_ip"), tf_output(INFRA_DIR, "idp_instance_id")
    if not ip or not instance:
        die("could not read the IDP host from Terraform state")
    if not IPV4.fullmatch(ip) or not re.fullmatch(r"i-[0-9a-f]+", instance):
        die("no valid IDP host in local state")
    account = aws("sts", "get-caller-identity", "--query", "Account", "--output", "text")
    if not account.ok or account.out.strip() != AWS_ACCOUNT:
        die("could not verify the POC AWS account")
    try:
        actual = aws_json("ec2", "describe-instances", "--instance-ids", instance, "--filters",
                          "Name=tag:cf:poc,Values=true", "Name=tag:cf:module,Values=infra", "Name=tag:Name,Values=cf-idp",
                          "Name=instance-state-name,Values=running", "--query", "Reservations[].Instances[].PublicIpAddress")
    except Unknown as exc:
        die(f"could not look up the IDP host: {exc}")
    if actual != [ip]:
        die("Terraform IP does not match a running, owned IDP")
    return ip


def cmd_deploy_idp(args: list[str]) -> None:
    if len(args) > 1:
        die("usage: ./poc deploy-idp [<sha256:image-id>]")
    if args and not IMAGE_ID.fullmatch(args[0]):
        die("pass the full image ID printed by vendy-idp/deploy/build.sh")
    for tool in ("terraform", "aws", "docker", "curl", "ssh", "scp"):
        if shutil.which(tool) is None:
            die(f"missing tool: {tool}")
    if not nonempty(USERS_FILE):
        die("no IDP users; run ./poc set-password <user> first")
    if not nonempty(POC_ENV):
        die(f"missing {POC_ENV}")
    if not deploy_idp(existing_idp_ip(), "routine", args[0] if args else None):
        die("IDP deploy failed; see the output above")


def cmd_prune_idp_images(args: list[str]) -> None:
    if args:
        die("usage: ./poc prune-idp-images")
    if not run([str(IDP_DIR / "deploy/deploy.sh"), "--prune", existing_idp_ip()], capture=False).ok:
        die("IDP image pruning failed")


def idp_host() -> str:
    ip = tf_output(INFRA_DIR, "idp_ip")
    if not ip:
        die("no IDP host (run ./poc up)")
    return ip
