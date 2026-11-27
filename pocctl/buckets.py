"""Discovery and teardown of the Bucket product, separate from VM state storage."""

from __future__ import annotations

import json
import re
import time
from urllib.parse import unquote
from uuid import UUID

from .core import (
    AWS_ACCOUNT, Unknown, aws, aws_cmd, aws_error, aws_ids, aws_json, az, az_json,
    info, last_line, parallel, run, warn,
)

AWS_BUCKET_PREFIX = f"cf-bucket-{AWS_ACCOUNT}-"
AZURE_BUCKET_GROUP = "cf-buckets"
AZURE_BUCKET_ACCOUNT = f"cfbuckets{AWS_ACCOUNT}"
BUCKET_TAGS = {"cf:poc": "true", "cf:module": "buckets"}
AZURE_TAGS = {"cf:poc": "true", "cf:module": "infra", "cf:purpose": "buckets"}


def aws_buckets() -> list[str]:
    return aws_ids("s3api", "list-buckets", "--prefix", AWS_BUCKET_PREFIX, "--query", "Buckets[].Name")


def azure_bucket_group() -> bool:
    """Confirm the dedicated group contains only the expected owned storage account."""
    exists = az_json("group", "exists", "--name", AZURE_BUCKET_GROUP)
    if exists is False:
        return False
    if exists is not True:
        raise Unknown("could not establish the Azure Bucket resource group status")
    group = az_json("group", "show", "--name", AZURE_BUCKET_GROUP)
    resources = az_json("resource", "list", "--resource-group", AZURE_BUCKET_GROUP)
    if (not isinstance(group, dict) or not isinstance(group.get("tags"), dict)
            or not AZURE_TAGS.items() <= group["tags"].items() or not isinstance(resources, list)):
        raise Unknown(f"refusing to delete unverified Azure group {AZURE_BUCKET_GROUP}")
    for resource in resources:
        if (not isinstance(resource, dict) or resource.get("type", "").lower() != "microsoft.storage/storageaccounts"
                or resource.get("name") != AZURE_BUCKET_ACCOUNT or not isinstance(resource.get("tags"), dict)
                or not AZURE_TAGS.items() <= resource["tags"].items()):
            raise Unknown(f"unexpected or unowned resource in Azure group {AZURE_BUCKET_GROUP}")
    return True


def _purge_aws_bucket(name: str) -> bool:
    try:
        tail = name.removeprefix(AWS_BUCKET_PREFIX)
        if not name.startswith(AWS_BUCKET_PREFIX) or not re.fullmatch(r"[0-9a-f]{32}", tail):
            raise Unknown("name is outside the Vendy Bucket contract")
        scope = ("--bucket", name, "--expected-bucket-owner", AWS_ACCOUNT)
        tags = aws_json("s3api", "get-bucket-tagging", *scope, "--query", "TagSet")
        expected = {**BUCKET_TAGS, "cf:bucket-id": str(UUID(hex=tail))}
        if not isinstance(tags, list) or not expected.items() <= {
            tag.get("Key"): tag.get("Value") for tag in tags if isinstance(tag, dict)
        }.items():
            raise Unknown("ownership tags do not match the bucket ID")

        # CLI grants can outlive Backstage. Stop new writes and multipart completions
        # before emptying; the final DeleteBucket also refuses a nonempty bucket.
        freeze = {
            "Version": "2012-10-17", "Statement": [{
                "Effect": "Deny", "Principal": "*", "Action": "s3:PutObject",
                "Resource": f"arn:aws:s3:::{name}/*",
            }],
        }
        result = run(aws_cmd("s3api", "put-bucket-policy", *scope, "--policy", "file:///dev/stdin"),
                     input=json.dumps(freeze))
        if not result.ok:
            raise Unknown(f"could not freeze uploads: {last_line(result.err)}")

        deadline = time.monotonic() + 15 * 60
        while time.monotonic() < deadline:
            uploads = aws_json("s3api", "list-multipart-uploads", *scope, "--no-paginate", "--encoding-type", "url")
            if not isinstance(uploads, dict):
                raise Unknown("invalid multipart upload list")
            parts = uploads.get("Uploads") or []
            if parts:
                def abort_upload(entry: dict[str, str]) -> bool:
                    deleted = aws("s3api", "abort-multipart-upload", *scope,
                                  "--key", unquote(entry["Key"]), "--upload-id", entry["UploadId"])
                    return deleted.ok or aws_error(deleted.err, "NoSuchUpload")

                if not all(parallel(abort_upload, parts, workers=8)):
                    raise Unknown("could not abort every multipart upload")
                continue

            versions = aws_json("s3api", "list-object-versions", *scope, "--no-paginate", "--encoding-type", "url")
            if not isinstance(versions, dict):
                raise Unknown("invalid object version list")
            objects = [{"Key": unquote(entry["Key"]), "VersionId": entry["VersionId"]}
                       for kind in ("Versions", "DeleteMarkers") for entry in versions.get(kind, [])]
            if not objects:
                current = aws_json("s3api", "list-objects-v2", *scope, "--no-paginate", "--encoding-type", "url")
                if not isinstance(current, dict):
                    raise Unknown("invalid object list")
                objects = [{"Key": unquote(entry["Key"])} for entry in current.get("Contents", [])]
            if objects:
                # A key allowed by S3 PUT can contain characters that cannot be represented in the
                # batch API's XML document. SDK/CLI path encoding handles those keys individually.
                xml_safe = re.compile(r"[\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]*\Z")
                normal = []
                for entry in objects:
                    key = entry["Key"]
                    if xml_safe.fullmatch(key):
                        normal.append(entry)
                    else:
                        args = ("--version-id", entry["VersionId"]) if "VersionId" in entry else ()
                        if not aws("s3api", "delete-object", *scope, f"--key={key}", *args).ok:
                            raise Unknown("could not remove an object with an XML-incompatible name")
                if normal:
                    result = run(aws_cmd("s3api", "delete-objects", *scope, "--delete", "file:///dev/stdin", "--output", "json"),
                                 input=json.dumps({"Objects": normal, "Quiet": True}))
                    if not result.ok or json.loads(result.out or "{}").get("Errors"):
                        raise Unknown("batch deletion of bucket data failed")
                continue

            result = aws("s3api", "delete-bucket", *scope)
            if result.ok or aws_error(result.err, "NoSuchBucket"):
                info(f"removed S3 bucket {name}")
                return True
            if not aws_error(result.err, "BucketNotEmpty"):
                raise Unknown(f"could not delete the empty bucket: {last_line(result.err)}")
        raise Unknown("bucket cleanup exceeded 15 minutes")
    except (Unknown, ValueError, KeyError, TypeError) as exc:
        warn(f"Bucket {name}: {exc}")
        return False


def purge_buckets(idp_stopped: bool, workers: int) -> bool:
    try:
        names, group = aws_buckets(), azure_bucket_group()
        if (names or group) and not idp_stopped:
            raise Unknown("could not verify the IDP stopped before Bucket teardown")
    except Unknown as exc:
        warn(str(exc))
        return False
    return all(parallel(_purge_aws_bucket, names, workers=min(workers, 4)))


def sweep_azure_bucket_group() -> bool:
    """Terraform owns the group; clean an owned orphan if local state was lost."""
    try:
        if not azure_bucket_group():
            return True
        info(f"deleting Azure Bucket group {AZURE_BUCKET_GROUP}")
        deleted = az("group", "delete", "--name", AZURE_BUCKET_GROUP, "--yes", "--no-wait")
        if deleted.ok and az("group", "wait", "--name", AZURE_BUCKET_GROUP, "--deleted", "--timeout", "600").ok:
            return True
        warn(f"could not verify deletion of Azure Bucket group {AZURE_BUCKET_GROUP}")
    except Unknown as exc:
        warn(str(exc))
    return False
