"""Cycle-owned credential for the persistent IDP Reader app. Never log secret values or tokens."""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import msal
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .core import (
    AWS_ACCOUNT, AWS_REGION, AZURE_LOCATION, AZURE_READER_CLIENT_ID, AZURE_READER_SECRET_ID, AZURE_SUBSCRIPTION, AZURE_TENANT,
    Unknown, aws_cmd, aws_error, aws_json, info, pause, require_terraform_azure_auth, run,
)

GRAPH = "https://graph.microsoft.com"
ARM = "https://management.azure.com"
READER_APP = f"{GRAPH}/v1.0/applications/c657f34c-eb44-4d2c-9abd-4a4dc359f4fa"
OWNED_NAME = "vendy-poc-reader:"
VERSION = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
IDENTITY = {
    "tenantId": AZURE_TENANT,
    "clientId": AZURE_READER_CLIENT_ID,
    "subscriptionId": AZURE_SUBSCRIPTION,
    "location": AZURE_LOCATION,
}


def _token(client_id: str, credential: dict[str, str], resource: str) -> str:
    try:
        app = msal.ConfidentialClientApplication(
            client_id, authority=f"https://login.microsoftonline.com/{AZURE_TENANT}",
            client_credential=credential, timeout=10,
        )
        result = app.acquire_token_for_client([f"{resource}/.default"])
    except (OSError, ValueError, requests.RequestException) as exc:
        raise Unknown(f"Azure authentication failed ({type(exc).__name__})") from None
    token = result.get("access_token")
    if not token:
        codes = result.get("error_codes", [])
        reported = ", ".join(str(code) for code in codes if isinstance(code, int))
        raise Unknown(f"Azure refused the certificate login{f' (AADSTS {reported})' if reported else ''}")
    return token


def operator_token() -> str:
    if not require_terraform_azure_auth():
        raise Unknown("configure the operator Azure certificate with ARM_*")
    return _token(os.environ["ARM_CLIENT_ID"], {
        "private_key_pfx_path": os.environ["ARM_CLIENT_CERTIFICATE_PATH"],
        "passphrase": os.environ["ARM_CLIENT_CERTIFICATE_PASSWORD"],
    }, GRAPH)


def _graph(method: str, token: str, *, keys: list[dict[str, Any]] | None = None) -> requests.Response:
    try:
        result = requests.request(
            method, READER_APP, params={"$select": "appId,keyCredentials"} if method == "GET" else None,
            headers={"Authorization": f"Bearer {token}"}, json={"keyCredentials": keys} if keys is not None else None,
            timeout=15, allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise Unknown(f"Microsoft Graph {method} Reader app failed ({type(exc).__name__})") from None
    if result.status_code != (200 if method == "GET" else 204):
        raise Unknown(f"Microsoft Graph {method} Reader app returned HTTP {result.status_code}")
    return result


def _keys(token: str) -> list[dict[str, Any]]:
    result = _graph("GET", token)
    try:
        app = result.json()
        keys = app["keyCredentials"]
    except (ValueError, KeyError, TypeError):
        raise Unknown("Microsoft Graph returned invalid Reader app credentials") from None
    if app.get("appId") != AZURE_READER_CLIENT_ID or not isinstance(keys, list) or not all(
        isinstance(key, dict) and isinstance(key.get("keyId"), str) for key in keys
    ):
        raise Unknown("could not verify the Reader app or its credentials")
    return keys


def _owned(key: dict[str, Any]) -> bool:
    name = key.get("displayName")
    return (isinstance(name, str) and name.startswith(OWNED_NAME)
            and VERSION.fullmatch(name[len(OWNED_NAME):]) is not None
            and key.get("type") == "AsymmetricX509Cert" and key.get("usage") == "Verify")


def _replace_keys(token: str, keys: list[dict[str, Any]]) -> None:
    # Graph PATCH replaces this collection. Refuse to write incomplete retained credentials.
    if any(not key.get("key") or not key.get("type") or not key.get("usage") for key in keys):
        raise Unknown("Microsoft Graph did not return all public key material; Reader credentials left unchanged")
    fields = {"keyId", "key", "type", "usage", "customKeyIdentifier", "startDateTime", "endDateTime", "displayName"}
    _graph("PATCH", token, keys=[{k: v for k, v in key.items() if k in fields and v is not None} for key in keys])


def check_reader_app(token: str, resume: bool) -> None:
    if any(_owned(key) for key in _keys(token)) and not resume:
        raise Unknown("managed Azure Reader certificates already exist; finish ./poc down before starting a new cycle")


def _new_secret() -> dict[str, str]:
    version = str(uuid4())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "cf-idp-azure-reader")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=364)).add_extension(x509.BasicConstraints(ca=False, path_length=None),
                                                                     critical=True).sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    )
    return {**IDENTITY, "credentialId": version, "certificatePem": pem.decode()}


def _secret() -> tuple[str, dict[str, Any] | None]:
    secret = aws_json("secretsmanager", "describe-secret", "--secret-id", AZURE_READER_SECRET_ID)
    arn_prefix = f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT}:secret:{AZURE_READER_SECRET_ID}-"
    if not isinstance(secret, dict) or "DeletedDate" in secret:
        raise Unknown("could not establish ownership of the Azure Reader secret")
    arn, tags = secret.get("ARN"), secret.get("Tags")
    if (not isinstance(arn, str) or not arn.startswith(arn_prefix) or not isinstance(tags, list)
            or {"cf:poc": "true", "cf:module": "infra"}.items() - {
                tag.get("Key"): tag.get("Value") for tag in tags if isinstance(tag, dict)
            }.items()):
        raise Unknown("could not establish ownership of the Azure Reader secret")
    result = run(aws_cmd("secretsmanager", "get-secret-value", "--secret-id", arn,
                         "--version-stage", "AWSCURRENT", "--output", "json"))
    if not result.ok:
        if not secret.get("VersionIdsToStages") and aws_error(result.err, "ResourceNotFoundException"):
            return arn, None
        raise Unknown("could not read the current Azure Reader secret value")
    try:
        response = json.loads(result.out)
        value = json.loads(response["SecretString"])
        version = value.get("credentialId")
        if (any(value.get(k) != v for k, v in IDENTITY.items()) or not isinstance(value["certificatePem"], str)
                or (version is not None and (not isinstance(version, str) or not VERSION.fullmatch(version)
                                             or response["VersionId"] != version))):
            raise ValueError
    except (AttributeError, KeyError, ValueError, TypeError):
        raise Unknown("Azure Reader secret does not match the expected credential contract") from None
    return arn, value


def _certificate(value: dict[str, Any]) -> tuple[x509.Certificate, str]:
    try:
        pem = value["certificatePem"].encode()
        cert = x509.load_pem_x509_certificate(pem)
        key = serialization.load_pem_private_key(pem, password=None)
        public_key = cert.public_key()
        if (not isinstance(key, rsa.RSAPrivateKey) or not isinstance(public_key, rsa.RSAPublicKey)
                or key.public_key().public_numbers() != public_key.public_numbers()
                or cert.not_valid_after_utc <= datetime.now(timezone.utc)):
            raise ValueError
        private_key = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ).decode()
    except (TypeError, ValueError):
        raise Unknown("Azure Reader certificate is invalid, mismatched, or expired; tear down and start a new cycle") from None
    return cert, private_key


def ensure_reader(token: str) -> None:
    arn, value = _secret()
    if value is None:
        # Save before registering. A failed or interrupted Graph write leaves a durable retry record;
        # down can find any registered key by its managed name without needing the secret.
        new = _new_secret()
        result = run(aws_cmd("secretsmanager", "put-secret-value", "--secret-id", arn, "--client-request-token",
                             new["credentialId"], "--secret-string", "file:///dev/stdin"), input=json.dumps(new))
        if result.ok:
            value = new
        else:
            _, value = _secret()
            if value is None or value.get("credentialId") != new["credentialId"]:
                raise Unknown("could not store the generated Azure Reader credential")
    cert, private_key = _certificate(value)
    version = value.get("credentialId")
    if version is not None:
        keys = _keys(token)
        name = OWNED_NAME + version
        public = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
        managed = [key for key in keys if _owned(key)]
        if managed:
            if len(managed) != 1 or managed[0]["displayName"] != name or managed[0].get("key") != public:
                raise Unknown("registered Azure Reader certificate does not match the stored cycle credential")
        else:
            _replace_keys(token, [*keys, {
                "keyId": version, "displayName": name, "key": public, "type": "AsymmetricX509Cert", "usage": "Verify",
                "startDateTime": cert.not_valid_before_utc.isoformat(), "endDateTime": cert.not_valid_after_utc.isoformat(),
            }])
    for attempt in range(6):
        try:
            _token(AZURE_READER_CLIENT_ID, {
                "private_key": private_key, "thumbprint": cert.fingerprint(hashes.SHA1()).hex(),
            }, ARM)
            info("Azure Reader certificate authenticated")
            return
        except Unknown:
            if attempt == 5 or pause(5):
                raise


def revoke_reader() -> None:
    token = operator_token()
    keys = _keys(token)
    retained = [key for key in keys if not _owned(key)]
    if len(retained) < len(keys):
        _replace_keys(token, retained)
        for attempt in range(4):
            if not any(_owned(key) for key in _keys(token)):
                info("revoked managed Azure Reader certificates")
                return
            if attempt == 3 or pause(2):
                raise Unknown("managed Azure Reader certificate revocation could not be verified")
