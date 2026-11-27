"""Owned-resource sweeps, the VM state bucket, and absence checks for AWS, Azure and DNS."""

from __future__ import annotations

import datetime
import json
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from .buckets import AWS_BUCKET_PREFIX, AZURE_BUCKET_GROUP
from .core import (
    AWS_ACCOUNT, AWS_REGION, AZURE_READER_SECRET_ID, CLEAN_MAX_ATTEMPTS, CLEAN_RETRY_SECONDS, DOMAIN, GEN, INFRA_DIR,
    IMAGES_DIR, LOGS,
    STATE_BUCKET, Unknown, aws, aws_cmd, aws_error, aws_ids, aws_json, az, az_cmd, az_json, delete_aws, info,
    last_line, parallel, pause, require_terraform_azure_auth, root_state, run, warn,
)
from .github import verify_quiescent
from .idp import save_caddy_data

POC = "Name=tag:cf:poc,Values=true"
LIVE_STATES = "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down"
NOT_INFRA = "!(Tags[?Key==`cf:module`&&Value==`infra`])"
AZ_POC = "tags.\"cf:poc\"=='true'"
STATE_KEY = re.compile(r"vms/[a-z][a-z0-9-]*/[a-z][a-z0-9-]{2,20}/terraform\.tfstate(\.tflock)?")
LOCK_TIME = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)")


def owned_tags(module: str, tags: object) -> bool:
    if not isinstance(tags, list):
        return False
    pairs = {(t.get("Key"), t.get("Value")) for t in tags if isinstance(t, dict)}
    return ("cf:poc", "true") in pairs and ("cf:module", module) in pairs


# ---------------------------------------------------------------- state bucket


def find_state_bucket() -> str:
    """Match an account-owned bucket by exact name. HeadBucket cannot distinguish absent from inaccessible."""
    names = aws_ids("s3api", "list-buckets", "--prefix", STATE_BUCKET, "--query", f"Buckets[?Name=='{STATE_BUCKET}'].Name")
    return names[0] if names else ""


def state_bucket_keys(require_present: bool = False) -> list[str] | None:
    """Object keys of the owned state bucket; [] if absent (None if require_present); Unknown if unprovable."""
    owned = find_state_bucket()
    if not owned:
        return None if require_present else []
    if owned != STATE_BUCKET:
        raise Unknown(f"unexpected bucket {owned}")
    try:
        tags = aws_json("s3api", "get-bucket-tagging", "--bucket", STATE_BUCKET, "--query", "TagSet")
    except Unknown:
        tags = None
    if not owned_tags("infra", tags):
        warn(f"cannot establish ownership of bucket {STATE_BUCKET}")
        raise Unknown(f"cannot establish ownership of bucket {STATE_BUCKET}")
    data = aws_json("s3api", "list-objects-v2", "--bucket", STATE_BUCKET, allow_empty=True)
    return [item["Key"] for item in (data or {}).get("Contents") or []]


def _lock_epoch(created: str) -> float:
    match = LOCK_TIME.fullmatch(created)
    if not match:
        raise ValueError(f"unexpected lock time {created}")
    zone = "+00:00" if match.group(3) == "Z" else match.group(3)
    return datetime.datetime.fromisoformat(match.group(1) + zone).timestamp()


def prepare_vm_states(scratch: Path) -> list[tuple[str, str, str]] | None:
    """(platform, name, lock id or "-") per VM state, or None when state/lock safety cannot be shown."""
    try:
        keys = state_bucket_keys()
    except Unknown:
        warn(f"cannot inventory {STATE_BUCKET}")
        return None
    if not keys:
        return []
    for key in keys:
        if not STATE_KEY.fullmatch(key):
            warn(f"unexpected key in {STATE_BUCKET}: {key}; refusing to assume it is safe to sweep")
            return None
    if any(key.endswith(".tflock") for key in keys):
        result = run(aws_cmd("s3", "sync", f"s3://{STATE_BUCKET}/vms/", f"{scratch}/", "--exclude", "*",
                             "--include", "*/terraform.tfstate.tflock", "--only-show-errors"))
        if not result.ok:
            warn(f"could not read VM state locks: {last_line(result.err)}")
            return None
    now = time.time()
    plan = []
    for key in sorted({k.removesuffix(".tflock") for k in keys}):
        _, platform, name, _ = key.split("/")
        lock_id = "-"
        if f"{key}.tflock" in keys:
            try:
                lock = json.loads((scratch / platform / name / "terraform.tfstate.tflock").read_text())
                lock_id, created = lock["ID"], lock["Created"]
                if not isinstance(lock_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", lock_id):
                    raise ValueError("unexpected lock ID")
                epoch = _lock_epoch(created)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                warn(f"cannot read the {platform}/{name} state lock: {exc}")
                return None
            if now - epoch <= 1200:
                warn(f"{platform}/{name} has a recent state lock; retry after the operator stops or the "
                     "interrupted runner lock is older than 20 minutes")
                return None
        plan.append((platform, name, lock_id))
    return plan


def destroy_vms(plan: list[tuple[str, str, str]], mirror: Path, platform_dir: Path, workers: int) -> bool:
    if not plan:
        info("no VM state")
        return True
    mirror.mkdir(parents=True, exist_ok=True)
    # Populate one mirror serially, then each worker uses it read-only.
    for platform in sorted({p for p, _, _ in plan}):
        root = platform_dir / "terraform/platforms" / platform
        if not root.is_dir():
            warn(f"no Terraform root for platform {platform} in {platform_dir}")
            return False
        path = LOGS / f"mirror-{platform}.log"
        if not run(["terraform", f"-chdir={root}", "providers", "mirror", "-var", "name=poc-mirror", str(mirror)], log=path).ok:
            warn(f"provider mirror failed: {path}")
            return False
    azure_ok = require_terraform_azure_auth() if any(p == "azure" for p, _, _ in plan) else True
    results = parallel(lambda vm: _destroy_vm(mirror, platform_dir, *vm, azure_ok=azure_ok), plan, workers)
    return all(results)


def _destroy_vm(mirror: Path, platform_dir: Path, platform: str, name: str, lock_id: str, azure_ok: bool) -> bool:
    root = platform_dir / "terraform/platforms" / platform
    path = LOGS / f"vm-{platform}-{name}.log"
    if platform == "azure" and not azure_ok:
        warn(f"[{platform}/{name}] no Terraform Azure authentication; keeping state")
        return False
    data_dir = tempfile.mkdtemp(dir=GEN, prefix="tf-vm.")
    key = f"vms/{platform}/{name}/terraform.tfstate"
    terraform = ["terraform", f"-chdir={root}"]
    steps = [[*terraform, "init", "-input=false", "-reconfigure", "-lockfile=readonly", f"-plugin-dir={mirror}",
              f"-backend-config=bucket={STATE_BUCKET}", f"-backend-config=key={key}",
              f"-backend-config=region={AWS_REGION}", "-backend-config=use_lockfile=true"]]
    if lock_id != "-":
        steps.append([*terraform, "force-unlock", "-force", lock_id])
    steps.append([*terraform, "destroy", "-auto-approve", "-input=false", "-var", f"name={name}"])
    steps.append(aws_cmd("s3api", "delete-object", "--bucket", STATE_BUCKET, "--key", key))
    try:
        for index, step in enumerate(steps):
            if not run(step, env={"TF_DATA_DIR": data_dir, "TF_PLUGIN_CACHE_DIR": None}, log=path, append=index > 0).ok:
                warn(f"[{platform}/{name}] teardown failed; preserving state (see {path})")
                return False
        info(f"[{platform}/{name}] destroyed")
        return True
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def remove_empty_state_bucket() -> bool:
    try:
        keys = state_bucket_keys()
        if keys:
            warn("state bucket still has VM state/lock objects")
            return False
        if not find_state_bucket():
            return True
        versions = aws_json("s3api", "list-object-versions", "--bucket", STATE_BUCKET, allow_empty=True) or {}
    except Unknown as exc:
        warn(str(exc))
        return False
    if (versions.get("Versions") or []) or (versions.get("DeleteMarkers") or []):
        warn("state bucket still has objects or versions; it will not be removed")
        return False
    # Delete it without force before the shared destroy, including when older local Terraform
    # state still records force_destroy=true on the bucket.
    return delete_aws(f"state bucket {STATE_BUCKET}", "NoSuchBucket", "s3api", "delete-bucket", "--bucket", STATE_BUCKET)


# ---------------------------------------------------------------- sweeps


def stop_idp() -> bool:
    try:
        instances = aws_json("ec2", "describe-instances", "--filters", "Name=tag:Name,Values=cf-idp", POC,
                             "Name=tag:cf:module,Values=infra",
                             "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down",
                             "--query", "Reservations[].Instances[].[InstanceId,State.Name,PublicIpAddress]")
    except Unknown as exc:
        warn(f"could not discover owned IDP instances: {exc}")
        return False
    ok = True
    for instance_id, state, ip in instances or []:
        if state in ("stopped", "shutting-down", "terminated"):
            pass
        elif state == "stopping":
            if not aws("ec2", "wait", "instance-stopped", "--instance-ids", instance_id).ok:
                ok = False
                continue
        elif state in ("pending", "running"):
            if state == "running" and ip and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", ip):
                if not save_caddy_data(ip):
                    warn("could not save TLS storage; keeping any previous backup")
            if state == "pending" and not aws("ec2", "wait", "instance-running", "--instance-ids", instance_id).ok:
                ok = False
                continue
            if (not aws("ec2", "stop-instances", "--instance-ids", instance_id).ok
                    or not aws("ec2", "wait", "instance-stopped", "--instance-ids", instance_id).ok):
                ok = False
                continue
        else:
            warn(f"unexpected IDP instance state '{state}'")
            ok = False
            continue
        info(f"IDP instance quiesced: {instance_id} ({state})")
    return ok


def sweep_tagged(exclude_infra: bool, keep_images: bool) -> bool:
    """Remove POC-tagged EC2 leftovers in the pinned region."""
    failed = False
    info(f"sweeping AWS region {AWS_REGION}")

    def listed(label: str, *args: str) -> list[str]:
        nonlocal failed
        try:
            return aws_ids(*args)
        except Unknown as exc:
            warn(f"could not list {label}: {exc}")
            failed = True
            return []

    def query(collection: str, field: str, tags: str = "Tags") -> str:
        if not exclude_infra:
            return f"{collection}[].{field}"
        return f"{collection}[?!({tags}[?Key==`cf:module`&&Value==`infra`])].{field}"

    instance_query = (f"Reservations[].Instances[] | [?{NOT_INFRA}].InstanceId" if exclude_infra
                      else "Reservations[].Instances[].InstanceId")
    instances = sorted(set(listed("instances", "ec2", "describe-instances", "--filters", POC, LIVE_STATES, "--query", instance_query)))
    if instances:
        info(f"terminating instances: {' '.join(instances)}")
        if (not aws("ec2", "terminate-instances", "--instance-ids", *instances).ok
                or not aws("ec2", "wait", "instance-terminated", "--instance-ids", *instances).ok):
            failed = True

    if not keep_images:
        for ami in listed("AMIs", "ec2", "describe-images", "--include-disabled", "--include-deprecated", "--owners", "self",
                          "--filters", POC, "--query", query("Images", "ImageId")):
            failed |= not delete_aws(f"AMI {ami}", "InvalidAMIID.NotFound", "ec2", "deregister-image", "--image-id", ami)
        # First deregister all owned AMIs; only delete independently owned snapshots.
        for snapshot in listed("snapshots", "ec2", "describe-snapshots", "--owner-ids", "self", "--filters", POC,
                               "--query", query("Snapshots", "SnapshotId")):
            failed |= not delete_aws(f"snapshot {snapshot}", "InvalidSnapshot.NotFound", "ec2", "delete-snapshot",
                                     "--snapshot-id", snapshot)

    for volume in listed("volumes", "ec2", "describe-volumes", "--filters", POC, "Name=status,Values=available",
                         "--query", query("Volumes", "VolumeId")):
        failed |= not delete_aws(f"volume {volume}", "InvalidVolume.NotFound", "ec2", "delete-volume", "--volume-id", volume)
    for eni in listed("network interfaces", "ec2", "describe-network-interfaces", "--filters", POC, "Name=status,Values=available",
                      "--query", query("NetworkInterfaces", "NetworkInterfaceId", "TagSet")):
        failed |= not delete_aws(f"network interface {eni}", "InvalidNetworkInterfaceID.NotFound", "ec2",
                                 "delete-network-interface", "--network-interface-id", eni)
    for allocation in listed("EIPs", "ec2", "describe-addresses", "--filters", POC, "--query", query("Addresses", "AllocationId")):
        failed |= not delete_aws(f"EIP {allocation}", "InvalidAllocationID.NotFound", "ec2", "release-address",
                                 "--allocation-id", allocation)
    for group in listed("security groups", "ec2", "describe-security-groups", "--filters", POC,
                        "--query", query("SecurityGroups", "GroupId")):
        for _ in range(6):  # ENIs of just-terminated instances take a moment to go away
            result = aws("ec2", "delete-security-group", "--group-id", group)
            if result.ok or aws_error(result.err, "InvalidGroup.NotFound"):
                info(f"deleted {group}")
                break
            if "(DependencyViolation)" not in result.err or pause(10):
                break
        check = aws("ec2", "describe-security-groups", "--group-ids", group)
        if check.ok:
            warn(f"security group {group} still exists after delete attempts")
            failed = True
        elif not aws_error(check.err, "InvalidGroup.NotFound"):
            warn(f"could not verify security group {group}: {last_line(check.err)}")
            failed = True
    for key in listed("key pairs", "ec2", "describe-key-pairs", "--filters", POC, "--query", query("KeyPairs", "KeyPairId")):
        failed |= not delete_aws(f"key pair {key}", "InvalidKeyPair.NotFound", "ec2", "delete-key-pair", "--key-pair-id", key)
    # Windows VM login passwords (platform scripts/vm-password.sh); a VM destroy normally removes its own.
    for secret in listed("VM password secrets", "secretsmanager", "list-secrets", "--filters",
                         "Key=name,Values=compute-factory/vm/", "Key=tag-key,Values=cf:poc", "--query",
                         "SecretList[?starts_with(Name, `compute-factory/vm/`) && Tags[?Key==`cf:poc`&&Value==`true`]].Name"):
        failed |= not delete_aws(f"VM password secret {secret}", "ResourceNotFoundException", "secretsmanager", "delete-secret",
                                 "--secret-id", secret, "--force-delete-without-recovery")
    return not failed


def sweep_azure(exclude_infra: bool, keep_gallery: bool) -> bool:
    """Every VM and Packer build has its own resource group tagged cf:poc=true; the image tier's
    gallery group cf-images is cf:module=images."""
    condition = f"{AZ_POC} && name!='{AZURE_BUCKET_GROUP}'"
    if exclude_infra:
        condition += " && tags.\"cf:module\"!='infra'"
    if keep_gallery:
        condition += " && tags.\"cf:module\"!='images' && name!='cf-images'"
    try:
        groups = az_json("group", "list", "--query", f"[?{condition}].name")
    except Unknown as exc:
        warn(f"could not list Azure resource groups: {exc}")
        return False
    failed, queued = False, []
    for group in groups:
        result = az("group", "delete", "-n", group, "--yes", "--no-wait")
        if result.ok:
            queued.append(group)
            info(f"deleting resource group {group}")
        else:
            warn(f"could not delete resource group {group}: {last_line(result.err)}")
            failed = True
    waits = parallel(lambda g: az("group", "wait", "-n", g, "--deleted", "--timeout", "1800").ok, queued)
    return not failed and all(waits)


def delete_image_versions() -> bool:
    """Packer publishes gallery image versions outside Terraform; a definition cannot be deleted with them."""
    try:
        exists = az("group", "exists", "-n", "cf-images", "-o", "tsv")
        if not exists.ok:
            raise Unknown(last_line(exists.err))
        if exists.out.strip() == "false":
            return True
        if exists.out.strip() != "true":
            warn(f"unexpected Azure response while checking for cf-images: {exists.out.strip()}")
            return False
        tags = az_json("group", "show", "-n", "cf-images", "--query", "tags") or {}
        if tags.get("cf:poc") != "true" or tags.get("cf:module") != "images":
            warn("cannot establish ownership of Azure resource group cf-images")
            return False
        if not az_json("sig", "list", "-g", "cf-images", "--query", "[?name=='cfimages'].name"):
            return True
        definitions = az_json("sig", "image-definition", "list", "-g", "cf-images", "-r", "cfimages", "--query", "[].name")
    except Unknown as exc:
        warn(str(exc))
        return False

    def delete_definition_versions(definition: str) -> bool:
        try:
            versions = az_json("sig", "image-version", "list", "-g", "cf-images", "-r", "cfimages", "-i", definition,
                               "--query", "[].name")
        except Unknown as exc:
            warn(str(exc))
            return False
        ok = True
        for version in versions:
            info(f"deleting Azure gallery image version {definition}/{version} (can take several minutes)")
            ok &= az("sig", "image-version", "delete", "-g", "cf-images", "-r", "cfimages", "-i", definition, "-e", version).ok
        return ok

    # Definitions are independent; versions within one definition are deleted one at a time.
    return all(parallel(delete_definition_versions, definitions))


def sweep_infra_orphans(sweep_ca: bool) -> bool:
    """Fallback when Terraform state was lost. Names are not sufficient proof of ownership."""
    ok = True
    for secret in ("compute-factory/ssh-ca", AZURE_READER_SECRET_ID):
        module = "infra"
        if secret == "compute-factory/ssh-ca":
            if not sweep_ca:
                continue
            module = "images"
        result = aws("secretsmanager", "describe-secret", "--secret-id", secret, "--output", "json")
        if result.ok:
            data = json.loads(result.out)
            if data.get("DeletedDate") is not None:
                continue
            if not owned_tags(module, data.get("Tags")) or not delete_aws(
                    secret, "ResourceNotFoundException", "secretsmanager", "delete-secret", "--secret-id", secret,
                    "--force-delete-without-recovery"):
                warn(f"{secret} not removed; check its ownership tags and deletion error")
                ok = False
        elif not aws_error(result.err, "ResourceNotFoundException"):
            warn(f"cannot inspect {secret}: {last_line(result.err)}")
            ok = False
    if sweep_ca:
        result = aws("ssm", "list-tags-for-resource", "--resource-type", "Parameter", "--resource-id",
                     "/compute-factory/ssh-ca-pub", "--output", "json")
        if result.ok:
            if not owned_tags("images", json.loads(result.out).get("TagList")) or not delete_aws(
                    "CA public parameter", "ParameterNotFound", "ssm", "delete-parameter", "--name", "/compute-factory/ssh-ca-pub"):
                warn("CA public parameter not removed; check its ownership tags and deletion error")
                ok = False
        elif not aws_error(result.err, "InvalidResourceId") and not aws_error(result.err, "ParameterNotFound"):
            warn(f"cannot inspect CA public parameter: {last_line(result.err)}")
            ok = False
    result = aws("iam", "get-instance-profile", "--instance-profile-name", "cf-idp", "--output", "json")
    if result.ok:
        profile = json.loads(result.out).get("InstanceProfile") or {}
        roles = profile.get("Roles") or []
        if owned_tags("infra", profile.get("Tags")) and all(r.get("RoleName") == "cf-idp" for r in roles):
            if roles:
                ok &= delete_aws("cf-idp profile role binding", "NoSuchEntity", "iam", "remove-role-from-instance-profile",
                                 "--instance-profile-name", "cf-idp", "--role-name", "cf-idp")
            ok &= delete_aws("cf-idp instance profile", "NoSuchEntity", "iam", "delete-instance-profile",
                             "--instance-profile-name", "cf-idp")
        else:
            warn("cannot establish ownership of instance profile cf-idp or its role attachment")
            ok = False
    elif not aws_error(result.err, "NoSuchEntity"):
        warn(f"cannot inspect instance profile cf-idp: {last_line(result.err)}")
        ok = False
    result = aws("iam", "get-role", "--role-name", "cf-idp", "--output", "json")
    if result.ok:
        if owned_tags("infra", (json.loads(result.out).get("Role") or {}).get("Tags")):
            try:
                inline = aws_ids("iam", "list-role-policies", "--role-name", "cf-idp", "--query", "PolicyNames")
                attached = aws_ids("iam", "list-attached-role-policies", "--role-name", "cf-idp",
                                   "--query", "AttachedPolicies[].PolicyArn")
            except Unknown as exc:
                warn(str(exc))
                inline, attached, ok = [], [], False
            for policy in inline:
                ok &= delete_aws(f"inline policy {policy}", "NoSuchEntity", "iam", "delete-role-policy", "--role-name", "cf-idp",
                                 "--policy-name", policy)
            for policy in attached:
                ok &= delete_aws(f"attachment {policy}", "NoSuchEntity", "iam", "detach-role-policy", "--role-name", "cf-idp",
                                 "--policy-arn", policy)
            ok &= delete_aws("cf-idp role", "NoSuchEntity", "iam", "delete-role", "--role-name", "cf-idp")
        else:
            warn("cannot establish ownership of IAM role cf-idp")
            ok = False
    elif not aws_error(result.err, "NoSuchEntity"):
        warn(f"cannot inspect IAM role cf-idp: {last_line(result.err)}")
        ok = False
    return ok


# ---------------------------------------------------------------- verification


def verify_absent(label: str, not_found: str, cmd: list[str]) -> bool:
    """List queries must succeed and return no matches. Point lookups must return their specific
    not-found error; permission, credential, and network failures are not evidence of absence."""
    result = run(cmd)
    if result.ok:
        if not result.out.strip():
            return True
        warn(f"{label} still present (or lookup returned unexpected output): {result.out.strip()}")
    elif not_found and aws_error(result.err, not_found):
        return True
    else:
        warn(f"could not verify {label}: {last_line(result.err)}")
    return False


def verify_dns_absent() -> bool:
    # The container wildcard (*.apps.<domain>) is checked through one name under it.
    return all(_dns_name_absent(name) for name in (DOMAIN, f"dns-check.apps.{DOMAIN}"))


def _dns_name_absent(name: str) -> bool:
    # DNS over HTTPS: networks such as the Netskope proxy block port 53 to public resolvers.
    result = run(["curl", "-fsS", "--max-time", "5", "-H", "accept: application/dns-json",
                  f"https://cloudflare-dns.com/dns-query?name={name}&type=A"])
    try:
        reply = json.loads(result.out) if result.ok else None
    except ValueError:
        reply = None
    if not isinstance(reply, dict) or reply.get("Status") not in (0, 3):
        warn(f"could not verify DNS for {name}: {result.out.strip() or last_line(result.err)}")
        return False
    if reply.get("Answer"):
        warn(f"DNS {name} still resolves (may be cached; TTL 60s)")
        return False
    return True


EC2_KINDS = {
    "instance": ("i", "instance-id", ["describe-instances", "--query", "Reservations[].Instances[].[InstanceId,State.Name]"]),
    "image": ("ami", "image-id", ["describe-images", "--owners", "self", "--include-disabled", "--include-deprecated",
                                  "--query", "Images[].ImageId"]),
    "snapshot": ("snap", "snapshot-id", ["describe-snapshots", "--owner-ids", "self", "--query", "Snapshots[].SnapshotId"]),
    "volume": ("vol", "volume-id", ["describe-volumes", "--query", "Volumes[].VolumeId"]),
    "network-interface": ("eni", "network-interface-id", ["describe-network-interfaces", "--query",
                                                          "NetworkInterfaces[].NetworkInterfaceId"]),
    "security-group": ("sg", "group-id", ["describe-security-groups", "--query", "SecurityGroups[].GroupId"]),
    "security-group-rule": ("sgr", "security-group-rule-id", ["describe-security-group-rules", "--query",
                                                              "SecurityGroupRules[].SecurityGroupRuleId"]),
    "elastic-ip": ("eipalloc", "allocation-id", ["describe-addresses", "--query", "Addresses[].AllocationId"]),
    "key-pair": ("key", "key-pair-id", ["describe-key-pairs", "--query", "KeyPairs[].KeyPairId"]),
}


def verify_tagged_ec2_gone(arns: list[str]) -> bool:
    """Batch same-type ARNs by exact ID without tag filters: a tag disappearing is not deletion."""
    prefix = f"arn:aws:ec2:{AWS_REGION}:{AWS_ACCOUNT}:"
    if not arns or not arns[0].startswith(prefix):
        return False
    kind = arns[0][len(prefix):].split("/", 1)[0]
    if kind not in EC2_KINDS:
        return False
    id_prefix, id_filter, args = EC2_KINDS[kind]
    ids = []
    for arn in arns:
        if not arn.startswith(f"{prefix}{kind}/"):
            return False
        resource = arn[len(f"{prefix}{kind}/"):]
        if not re.fullmatch(rf"{id_prefix}-[0-9a-f]+", resource):
            return False
        ids.append(resource)
    try:
        found = aws_json("ec2", *args, "--filters", f"Name={id_filter},Values={','.join(ids)}")
    except Unknown as exc:
        warn(f"could not confirm EC2 {kind} IDs {','.join(ids)}: {exc}")
        return False
    if not isinstance(found, list):
        return False
    if kind == "instance":
        return all(isinstance(item, list) and len(item) == 2 and item[1] == "terminated" for item in found)
    return not found


def verify_tagged_secrets_gone(arns: list[str]) -> bool:
    """Stale tag-index entries for deleted Windows VM password secrets (compute-factory/vm/<name>)."""
    prefix = f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT}:secret:compute-factory/vm/"
    for arn in arns:
        if not arn.startswith(prefix):
            return False
        result = aws("secretsmanager", "describe-secret", "--secret-id", arn, "--output", "json")
        if result.ok:
            if json.loads(result.out).get("DeletedDate") is None:
                return False
        elif not aws_error(result.err, "ResourceNotFoundException"):
            return False
    return True


def _tag_index_clean(allow_images: bool) -> bool:
    try:
        mappings = aws_json("resourcegroupstaggingapi", "get-resources", "--tag-filters", "Key=cf:poc,Values=true",
                            "--query", "ResourceTagMappingList")
    except Unknown as exc:
        warn(f"could not read the AWS {AWS_REGION} tag index: {exc}")
        return False
    if not isinstance(mappings, list) or not all(
        isinstance(m, dict) and isinstance(m.get("ResourceARN"), str) and isinstance(m.get("Tags"), list)
        and re.fullmatch(r"arn:\S+", m["ResourceARN"]) for m in mappings
    ):
        warn(f"could not parse the AWS {AWS_REGION} tag index")
        return False
    ec2 = f"arn:aws:ec2:{AWS_REGION}:{AWS_ACCOUNT}:"

    def retained(mapping: dict) -> bool:
        tags = {(t.get("Key"), t.get("Value")) for t in mapping["Tags"]}
        arn = mapping["ResourceARN"]
        return ("cf:module", "images") in tags or (
            (arn.startswith(f"{ec2}image/") or arn.startswith(f"{ec2}snapshot/")) and ("cf:managed", "true") in tags)

    groups: dict[str, list[str]] = {}
    for mapping in mappings:
        if allow_images and retained(mapping):
            continue
        groups.setdefault(mapping["ResourceARN"].split("/")[0], []).append(mapping["ResourceARN"])
    ok = True
    for _, arns in sorted(groups.items()):
        if verify_tagged_ec2_gone(arns) or verify_tagged_secrets_gone(arns):
            info(f"ignoring stale AWS tag entries verified absent or terminated in EC2: {' '.join(arns)}")
        else:
            warn(f"AWS {AWS_REGION} tag index reports resources not verified gone: {' '.join(arns)}")
            ok = False
    return ok


def _azure_outside_image_tier_clean() -> bool:
    result = run(az_cmd("resource", "list", "--tag", "cf:poc=true", "--query", "[].id", "-o", "json"))
    if not result.ok:
        warn(f"could not verify Azure tagged resources: {last_line(result.err)}")
        return False
    try:
        ids = json.loads(result.out)
    except ValueError:
        warn("could not parse Azure tagged resources")
        return False
    # Resource IDs differ in resource group case; compare case-insensitively.
    outside = [i for i in ids if "/resourcegroups/cf-images/" not in i.lower()]
    if outside:
        warn(f"Azure tagged resources outside cf-images: {' '.join(outside)}")
        return False
    return True


def verify_cloud_clean(allow_images: bool) -> bool:
    """allow_images: the image tier (SSH CA, cf-images gallery, golden AMIs and snapshots) may remain.
    Every check runs concurrently; all of them must pass."""
    region = AWS_REGION
    ami_query, snapshot_query = "Images[].ImageId", "Snapshots[].SnapshotId"
    if allow_images:
        # Raw string literals: a JMESPath `true` literal is a boolean and never equals the tag value.
        ami_query = "Images[?!(Tags[?Key=='cf:managed'&&Value=='true'])].ImageId"
        snapshot_query = "Snapshots[?!(Tags[?Key=='cf:managed'&&Value=='true'])].SnapshotId"
    text = ("--output", "text")
    checks: list[tuple[str, str, list[str]]] = [
        (f"AWS {region} instances", "", aws_cmd("ec2", "describe-instances", "--filters", POC, LIVE_STATES,
                                                "--query", "Reservations[].Instances[].InstanceId", *text)),
        (f"AWS {region} AMIs", "", aws_cmd("ec2", "describe-images", "--include-disabled", "--include-deprecated",
                                           "--owners", "self", "--filters", POC, "--query", ami_query, *text)),
        (f"AWS {region} snapshots", "", aws_cmd("ec2", "describe-snapshots", "--owner-ids", "self", "--filters", POC,
                                                "--query", snapshot_query, *text)),
        (f"AWS {region} volumes", "", aws_cmd("ec2", "describe-volumes", "--filters", POC, "--query", "Volumes[].VolumeId", *text)),
        (f"AWS {region} network interfaces", "", aws_cmd("ec2", "describe-network-interfaces", "--filters", POC,
                                                         "--query", "NetworkInterfaces[].NetworkInterfaceId", *text)),
        (f"AWS {region} security groups", "", aws_cmd("ec2", "describe-security-groups", "--filters", POC,
                                                      "--query", "SecurityGroups[].GroupId", *text)),
        (f"AWS {region} EIPs", "", aws_cmd("ec2", "describe-addresses", "--filters", POC, "--query", "Addresses[].PublicIp", *text)),
        (f"AWS {region} key pairs", "", aws_cmd("ec2", "describe-key-pairs", "--filters", POC, "--query", "KeyPairs[].KeyPairId", *text)),
        # A legacy temporary build might have failed before tags were written. Report candidates
        # rather than deleting by prefix, or silently claiming absence.
        (f"AWS {region} possible unowned Packer instances", "", aws_cmd(
            "ec2", "describe-instances", "--filters", "Name=key-name,Values=packer_*", LIVE_STATES,
            "--query", "Reservations[].Instances[].InstanceId", *text)),
        (f"AWS {region} possible unowned Packer security groups", "", aws_cmd(
            "ec2", "describe-security-groups", "--filters", "Name=group-name,Values=packer_*", "--query", "SecurityGroups[].GroupId", *text)),
        (f"AWS {region} possible unowned Packer key pairs", "", aws_cmd(
            "ec2", "describe-key-pairs", "--filters", "Name=key-name,Values=packer_*", "--query", "KeyPairs[].KeyName", *text)),
        ("AWS possible unowned cf-idp instances", "", aws_cmd(
            "ec2", "describe-instances", "--filters", "Name=tag:Name,Values=cf-idp", LIVE_STATES,
            "--query", "Reservations[].Instances[].InstanceId", *text)),
        ("AWS possible unowned cf-idp groups", "", aws_cmd(
            "ec2", "describe-security-groups", "--filters", "Name=group-name,Values=cf-idp", "--query", "SecurityGroups[].GroupId", *text)),
        ("AWS possible unowned cf-idp EIPs", "", aws_cmd(
            "ec2", "describe-addresses", "--filters", "Name=tag:Name,Values=cf-idp", "--query", "Addresses[].AllocationId", *text)),
        ("AWS possible unowned cf-idp-admin key", "", aws_cmd(
            "ec2", "describe-key-pairs", "--filters", "Name=key-name,Values=cf-idp-admin", "--query", "KeyPairs[].KeyPairId", *text)),
        ("state bucket", "", aws_cmd("s3api", "list-buckets", "--prefix", STATE_BUCKET,
                                     "--query", f"Buckets[?Name=='{STATE_BUCKET}'].Name", *text)),
        ("S3 Bucket product", "", aws_cmd("s3api", "list-buckets", "--prefix", AWS_BUCKET_PREFIX,
                                           "--query", "Buckets[].Name", *text)),
        ("Bucket upload role", "NoSuchEntity", aws_cmd(
            "iam", "get-role", "--role-name", "cf-bucket-upload", "--query", "Role.RoleName", *text)),
        ("Azure Bucket resource group", "", az_cmd(
            "group", "list", "--query", f"[?name=='{AZURE_BUCKET_GROUP}'].id", "-o", "tsv")),
        ("Azure Bucket secret", "ResourceNotFoundException", aws_cmd(
            "secretsmanager", "describe-secret", "--secret-id", "compute-factory/azure-buckets", "--query", "ARN", *text)),
        ("IAM role cf-idp", "NoSuchEntity", aws_cmd("iam", "get-role", "--role-name", "cf-idp", "--query", "Role.RoleName", *text)),
        ("IAM instance profile cf-idp", "NoSuchEntity", aws_cmd(
            "iam", "get-instance-profile", "--instance-profile-name", "cf-idp", "--query", "InstanceProfile.InstanceProfileName", *text)),
        ("Azure reader secret", "ResourceNotFoundException", aws_cmd(
            "secretsmanager", "describe-secret", "--secret-id", AZURE_READER_SECRET_ID, "--query", "ARN", *text)),
    ]
    if not allow_images:
        checks += [
            ("CA secret", "ResourceNotFoundException", aws_cmd(
                "secretsmanager", "describe-secret", "--secret-id", "compute-factory/ssh-ca", "--query", "ARN", *text)),
            ("CA public key parameter", "ParameterNotFound", aws_cmd(
                "ssm", "get-parameter", "--name", "/compute-factory/ssh-ca-pub", "--query", "Parameter.Name", *text)),
            ("Azure tagged groups", "", az_cmd("group", "list", "--query", f"[?{AZ_POC}].name", "-o", "tsv")),
            ("Azure cf-images group", "", az_cmd("group", "list", "--query", "[?name=='cf-images'].id", "-o", "tsv")),
            ("Azure tagged resources", "", az_cmd("resource", "list", "--tag", "cf:poc=true", "--query", "[].id", "-o", "tsv")),
        ]
    else:
        checks.append(("Azure tagged groups outside the image tier", "", az_cmd(
            "group", "list", "--query",
            f"[?{AZ_POC} && tags.\"cf:module\"!='images' && name!='cf-images'].name", "-o", "tsv")))
    def absent(check: tuple[str, str, list[str]]) -> Callable[[], bool]:
        return lambda: verify_absent(*check)

    tasks: list[Callable[[], bool]] = [absent(check) for check in checks]
    tasks += [lambda: _tag_index_clean(allow_images), verify_dns_absent]
    if allow_images:
        tasks.append(_azure_outside_image_tier_clean)
    return all(parallel(lambda task: task(), tasks, workers=12))


def verify_root_state_empty(root: Path, label: str) -> bool:
    try:
        state = root_state(root)
    except Unknown as exc:
        warn(f"cannot read {label}: {exc}")
        return False
    if state:
        warn(f"{label} is not empty: {' '.join(state)}")
        return False
    return True


def verify_infra_state_empty() -> bool:
    return verify_root_state_empty(INFRA_DIR, "local Terraform state")


def verify_images_state_empty() -> bool:
    return verify_root_state_empty(IMAGES_DIR, "local image tier Terraform state")


def verify_clean(allow_images: bool) -> bool:
    if not verify_infra_state_empty():
        return False
    if not allow_images and not verify_images_state_empty():
        return False
    for attempt in range(1, CLEAN_MAX_ATTEMPTS + 1):
        started = time.monotonic()
        if not verify_quiescent():
            return False
        if verify_cloud_clean(allow_images):
            if not verify_quiescent():
                return False
            info(f"cloud/DNS checks passed (AWS {AWS_REGION})")
            return True
        warn(f"cloud/DNS absence not yet verified (attempt {attempt}/{CLEAN_MAX_ATTEMPTS})")
        delay = CLEAN_RETRY_SECONDS - (time.monotonic() - started)
        if delay > 0 and attempt < CLEAN_MAX_ATTEMPTS and pause(delay):
            return False
    return False
