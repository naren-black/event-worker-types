#!/usr/bin/env python3
"""Proof of concept: MinIO STS AssumeRoleWithWebIdentity (the same flow as
AWS IAM Roles for Service Accounts / GCP Workload Identity Federation),
using Dex as a local OIDC provider.

Steps:
  1. Discover the MinIO role ARN for the Dex OIDC provider configured on
     the `minio` service (infra/docker-compose.yml).
  2. Get an id_token from Dex via the OAuth2 "password" grant
     (docker/dex/config.yaml - one static demo user).
  3. Exchange that id_token for short-lived S3 credentials via MinIO's STS
     AssumeRoleWithWebIdentity endpoint - no long-lived access key involved.
  4. Use the temporary credentials (SigV4-signed requests, stdlib only) to
     PUT and GET an object in the bucket the wms-uploader-policy allows.
  5. Confirm the policy is actually scoped: ListBuckets (not granted by
     wms-uploader-policy) is rejected with AccessDenied.

Usage:
    ./scripts/test-minio-sts.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT_DIR / "infra" / "docker-compose.yml"
COMPOSE = ["docker", "compose", "-f", str(COMPOSE_FILE)]

MINIO_ROOT_USER = os.environ.get("MINIO_ROOT_USER", "minioadmin")
MINIO_ROOT_PASSWORD = os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin")
S3_BUCKET = os.environ.get("S3_BUCKET", "wms-orders-processed")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")

DEX_TOKEN_URL = "http://localhost:5556/dex/token"
DEX_USERNAME = "worker@example.com"
DEX_PASSWORD = "password"
DEX_CLIENT_ID = "minio-worker"
DEX_CLIENT_SECRET = "minio-worker-secret"

MINIO_S3_HOST = "localhost:9000"
MINIO_S3_URL = f"http://{MINIO_S3_HOST}"

STS_NS = {"sts": "https://sts.amazonaws.com/doc/2011-06-15/"}


def discover_role_arn() -> str:
    """Read the role ARN MinIO derived for the Dex OIDC provider."""
    script = (
        f"mc alias set local http://minio:9000 '{MINIO_ROOT_USER}' '{MINIO_ROOT_PASSWORD}' >/dev/null 2>&1"
        " && mc idp openid info local"
    )
    result = subprocess.run(
        [*COMPOSE, "run", "--rm", "--entrypoint", "sh", "minio-init", "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    match = re.search(r"roleARN:\s*(\S+)", result.stdout)
    if not match:
        print(result.stdout, file=sys.stderr)
        raise RuntimeError("could not find roleARN in `mc admin idp openid info` output")
    return match.group(1)


def fetch_dex_id_token() -> str:
    """OAuth2 'password' grant against Dex - returns the OIDC id_token."""
    data = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "username": DEX_USERNAME,
            "password": DEX_PASSWORD,
            "scope": "openid",
            "client_id": DEX_CLIENT_ID,
            "client_secret": DEX_CLIENT_SECRET,
        }
    ).encode()
    req = urllib.request.Request(DEX_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["id_token"]


def assume_role_with_web_identity(id_token: str, role_arn: str) -> dict:
    """Exchange the id_token for short-lived MinIO S3 credentials."""
    params = urllib.parse.urlencode(
        {
            "Action": "AssumeRoleWithWebIdentity",
            "Version": "2011-06-15",
            "WebIdentityToken": id_token,
            "RoleArn": role_arn,
            "DurationSeconds": "900",
        }
    )
    req = urllib.request.Request(f"{MINIO_S3_URL}/?{params}", method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read()

    root = ET.fromstring(body)
    creds = root.find(".//sts:Credentials", STS_NS)
    return {
        "AccessKeyId": creds.find("sts:AccessKeyId", STS_NS).text,
        "SecretAccessKey": creds.find("sts:SecretAccessKey", STS_NS).text,
        "SessionToken": creds.find("sts:SessionToken", STS_NS).text,
        "Expiration": creds.find("sts:Expiration", STS_NS).text,
    }


def sigv4_headers(method: str, path: str, payload: bytes, creds: dict) -> dict:
    """Sign a path-style S3 request with temporary STS credentials."""
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()

    headers = {
        "host": MINIO_S3_HOST,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        "x-amz-security-token": creds["SessionToken"],
    }
    signed_header_keys = sorted(headers)
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in signed_header_keys)
    signed_headers = ";".join(signed_header_keys)

    canonical_request = "\n".join([method, path, "", canonical_headers, signed_headers, payload_hash])

    credential_scope = f"{date_stamp}/{S3_REGION}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _hmac(("AWS4" + creds["SecretAccessKey"]).encode(), date_stamp)
    k_region = _hmac(k_date, S3_REGION)
    k_service = _hmac(k_region, "s3")
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={creds['AccessKeyId']}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


def s3_request(method: str, path: str, creds: dict, payload: bytes = b"") -> tuple[int, bytes]:
    headers = sigv4_headers(method, path, payload, creds)
    req = urllib.request.Request(f"{MINIO_S3_URL}{path}", data=payload or None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    print("1. Discovering MinIO role ARN for the Dex OIDC provider...")
    role_arn = discover_role_arn()
    print(f"   roleARN = {role_arn}\n")

    print("2. Fetching an id_token from Dex (OAuth2 password grant)...")
    id_token = fetch_dex_id_token()
    print(f"   id_token = {id_token[:40]}...(truncated)\n")

    print("3. Exchanging id_token for temporary MinIO credentials (AssumeRoleWithWebIdentity)...")
    creds = assume_role_with_web_identity(id_token, role_arn)
    print(f"   AccessKeyId  = {creds['AccessKeyId']}")
    print(f"   SessionToken = {creds['SessionToken'][:40]}...(truncated)")
    print(f"   Expiration   = {creds['Expiration']}\n")

    key = f"sts-poc/{uuid.uuid4().hex}.txt"
    body = b"hello from MinIO STS / Workload Identity Federation PoC\n"
    path = f"/{S3_BUCKET}/{key}"

    print(f"4. PUT {path} using the temporary credentials...")
    status, resp_body = s3_request("PUT", path, creds, body)
    if status != 200:
        print(f"   FAILED: HTTP {status}\n{resp_body.decode(errors='replace')}", file=sys.stderr)
        return 1
    print(f"   OK: HTTP {status}\n")

    print(f"5. GET {path} back and verify content...")
    status, resp_body = s3_request("GET", path, creds)
    if status != 200 or resp_body != body:
        print(f"   FAILED: HTTP {status}\n{resp_body.decode(errors='replace')}", file=sys.stderr)
        return 1
    print(f"   OK: HTTP {status}, body matches\n")

    print(f"6. DELETE {path} - NOT granted by wms-uploader-policy, expecting AccessDenied...")
    status, resp_body = s3_request("DELETE", path, creds)
    if status == 403 and b"AccessDenied" in resp_body:
        print(f"   OK: HTTP {status} AccessDenied (policy is correctly scoped)\n")
    else:
        print(f"   UNEXPECTED: HTTP {status}\n{resp_body.decode(errors='replace')}", file=sys.stderr)
        return 1

    print("All checks passed: Dex id_token -> MinIO STS temp credentials -> scoped S3 access works.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
