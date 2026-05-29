"""KMD Nova helpers for MTM OO processes.

Nova authenticates via OAuth2 client-credentials (bearer tokens), and serves
its API behind a TLS cert chain that is missing a DigiCert intermediate from
most default trust stores. This module bundles three things:

* ``nova_request()`` — drop-in replacement for ``requests.request`` that
  catches the missing-intermediate SSL error and retries with an auto-patched
  CA bundle. Lifted verbatim from the legacy ``novaapi.py``.
* ``get_token()`` — refreshes the bearer token only when older than 90 min.
  Returns a ``TokenResult`` describing whether a refresh actually happened,
  so callers can write the new value back into OO themselves.
* ``get_document_list()`` and ``download_file()`` — wrappers around Nova's
  Document API endpoints.

All credentials are passed in by the caller; the lib has no dependency on
OpenOrchestrator.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import certifi
import pytz
import requests
from requests import Response
from requests.exceptions import SSLError


# ---------------------------------------------------------------------------
# TLS bundle handling (verbatim from legacy novaapi.py)
# ---------------------------------------------------------------------------

# Missing intermediate for novaapi.kmdnova.dk; served over plain HTTP by
# DigiCert via the cert's AIA URL.
DIGICERT_INTERMEDIATE_URL = (
    "http://cacerts.digicert.com/"
    "DigiCertGlobalG2TLSRSASHA2562020CA1-1.crt"
)

# Cache the patched bundle across runs on the same machine.
_CACHE_DIR = Path(os.getenv("PROGRAMDATA") or tempfile.gettempdir()) / "nova_tls_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_INTERMEDIATE_DER = _CACHE_DIR / "DigiCertGlobalG2TLSRSASHA2562020CA1-1.crt"
_INTERMEDIATE_PEM = _CACHE_DIR / "DigiCertGlobalG2TLSRSASHA2562020CA1-1.pem"
_COMBINED_BUNDLE = _CACHE_DIR / "nova_certifi_plus_intermediate.pem"


def _download_intermediate_der() -> Path:
    if _INTERMEDIATE_DER.exists() and _INTERMEDIATE_DER.stat().st_size > 0:
        return _INTERMEDIATE_DER
    r = requests.get(DIGICERT_INTERMEDIATE_URL, timeout=30)
    r.raise_for_status()
    _INTERMEDIATE_DER.write_bytes(r.content)
    return _INTERMEDIATE_DER


def _convert_der_to_pem(der_bytes: bytes) -> bytes:
    from cryptography import x509  # local import — cryptography is heavy
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_der_x509_certificate(der_bytes)
    return cert.public_bytes(serialization.Encoding.PEM)


def _ensure_intermediate_pem() -> Path:
    if _INTERMEDIATE_PEM.exists() and _INTERMEDIATE_PEM.stat().st_size > 0:
        return _INTERMEDIATE_PEM
    der_path = _download_intermediate_der()
    pem_bytes = _convert_der_to_pem(der_path.read_bytes())
    _INTERMEDIATE_PEM.write_bytes(pem_bytes)
    return _INTERMEDIATE_PEM


def ensure_nova_verify_bundle() -> str:
    """Return path to a CA bundle that includes the Nova-required intermediate."""
    if _COMBINED_BUNDLE.exists() and _COMBINED_BUNDLE.stat().st_size > 0:
        return str(_COMBINED_BUNDLE)
    base = Path(certifi.where()).read_bytes()
    intermediate = _ensure_intermediate_pem().read_bytes()
    if intermediate in base:
        _COMBINED_BUNDLE.write_bytes(base)
    else:
        _COMBINED_BUNDLE.write_bytes(base + b"\n" + intermediate)
    return str(_COMBINED_BUNDLE)


def nova_request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    json: dict | None = None,
    params: dict | None = None,
    timeout: int = 60,
    session: requests.Session | None = None,
    **kwargs,
) -> Response:
    """Drop-in replacement for ``requests.request`` that survives Nova's TLS chain.

    Tries with the normal certifi bundle first; if SSL verification fails with
    a chain error, retries once with a bundle that includes the missing
    DigiCert intermediate.
    """
    client = session or requests.Session()

    try:
        return client.request(
            method=method,
            url=url,
            headers=headers,
            json=json,
            params=params,
            timeout=timeout,
            verify=certifi.where(),
            **kwargs,
        )
    except SSLError as e:
        msg = str(e)
        chain_error = any(
            text in msg
            for text in (
                "CERTIFICATE_VERIFY_FAILED",
                "unable to get local issuer certificate",
                "unable to verify the first certificate",
            )
        )
        if not chain_error:
            raise
        verify_bundle = ensure_nova_verify_bundle()
        return client.request(
            method=method,
            url=url,
            headers=headers,
            json=json,
            params=params,
            timeout=timeout,
            verify=verify_bundle,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Token caching
# ---------------------------------------------------------------------------


_TOKEN_TIMESTAMP_FMT = "%d-%m-%Y %H:%M:%S"


@dataclass
class TokenResult:
    """Result of a ``get_token()`` call.

    ``refreshed`` tells the caller whether to write the new ``token`` /
    ``timestamp_str`` back to OO. If False, the existing OO values are still
    valid and should be left alone.
    """

    token: str
    timestamp_str: str
    refreshed: bool


def get_token(
    *,
    current_token: str | None,
    current_timestamp_str: str | None,
    token_url: str,
    client_id: str,
    client_secret: str,
    scope: str = "client",
    grant_type: str = "client_credentials",
    refresh_after_minutes: int = 90,
    tz_name: str = "Europe/Copenhagen",
) -> TokenResult:
    """Return a valid Nova bearer token, refreshing only when the cached one is stale.

    The caller supplies the current cached token + timestamp string (as stored
    in OO). If the timestamp is younger than ``refresh_after_minutes``, the
    cached token is returned with ``refreshed=False``. Otherwise a fresh token
    is fetched, and the caller should persist it back to OO.

    Timestamp format matches the legacy OO convention: ``dd-mm-yyyy HH:MM:SS``
    in the ``tz_name`` timezone.
    """
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)

    if current_token and current_timestamp_str:
        try:
            old = tz.localize(
                datetime.strptime(current_timestamp_str.strip(), _TOKEN_TIMESTAMP_FMT)
            )
            if (now - old) <= timedelta(minutes=refresh_after_minutes):
                return TokenResult(
                    token=current_token,
                    timestamp_str=current_timestamp_str,
                    refreshed=False,
                )
        except (ValueError, AttributeError):
            # Invalid timestamp string — fall through and refresh.
            pass

    body = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": scope,
        "grant_type": grant_type,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = nova_request("POST", token_url, headers=headers, data=body)
    if r.status_code != 200:
        raise RuntimeError(
            f"KMD token refresh failed: {r.status_code} {r.text[:200]!r}"
        )
    new_token = (r.json() or {}).get("access_token")
    if not new_token:
        raise RuntimeError("KMD token refresh: no access_token in response")

    return TokenResult(
        token=new_token,
        timestamp_str=now.strftime(_TOKEN_TIMESTAMP_FMT),
        refreshed=True,
    )


# ---------------------------------------------------------------------------
# Document API
# ---------------------------------------------------------------------------


_DEFAULT_DOC_GET_OUTPUT = {
    "documentType": True,
    "title": True,
    "caseWorker": True,
    "description": True,
    "fileExtension": True,
    "approved": True,
    "acceptReceived": True,
    "documentDate": True,
    "documentLevel": True,
    "numberOfSubDocuments": True,
    "subDocuments": True,
}


def get_case_metadata(
    *,
    token: str,
    base_url: str,
    case_number: str,
    case_get_output: dict | None = None,
) -> dict:
    """Return the first matching Nova case dict, or raise if not found."""
    url = f"{base_url}/Case/GetList?api-version=2.0-Case"
    payload = {
        "common": {"transactionId": str(uuid.uuid4())},
        "paging": {"startRow": 1, "numberOfRows": 5},
        "caseAttributes": {"userFriendlyCaseNumber": case_number},
        "caseGetOutput": case_get_output
        or {
            "caseAttributes": {
                "title": True,
                "userFriendlyCaseNumber": True,
                "numberOfDocuments": True,
            }
        },
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = nova_request("PUT", url, headers=headers, json=payload, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Nova case lookup failed: {r.status_code} {r.text[:200]!r}")
    cases = (r.json() or {}).get("cases") or []
    if not cases:
        raise RuntimeError(f"Nova case {case_number!r} not found")
    return cases[0]


def get_document_list(
    *,
    token: str,
    base_url: str,
    case_number: str,
    get_output: dict | None = None,
    page_size: int = 10000,
) -> list[dict]:
    """Return Nova documents grouped as ``[{'main': doc, 'subs': [doc, ...]}, ...]``.

    Each "main" document may have sub-documents (bilag). The Nova API returns
    only main docs in the case-level GetList; sub-documents must be fetched
    per-main via ``mainDocumentUuid``. This wrapper does both calls and groups
    the results so the caller gets a single nested structure to iterate.
    """
    url = f"{base_url}/Document/GetList?api-version=2.0-Case"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    output = get_output or _DEFAULT_DOC_GET_OUTPUT

    main_payload = {
        "common": {"transactionId": str(uuid.uuid4())},
        "paging": {
            "startRow": 0,
            "numberOfRows": page_size,
            "calculateTotalNumberOfRows": True,
        },
        "caseNumber": case_number,
        "getOutput": output,
    }
    r = nova_request("PUT", url, headers=headers, json=main_payload, timeout=300)
    r.raise_for_status()
    main_docs = (r.json() or {}).get("documents") or []

    groups: list[dict] = []
    for doc in main_docs:
        group = {"main": doc, "subs": []}
        if (doc.get("numberOfSubDocuments") or 0) > 0:
            sub_payload = {
                "common": {"transactionId": str(uuid.uuid4())},
                "paging": {
                    "startRow": 0,
                    "numberOfRows": page_size,
                    "calculateTotalNumberOfRows": True,
                },
                "mainDocumentUuid": doc["documentUuid"],
                "getOutput": output,
            }
            sr = nova_request("PUT", url, headers=headers, json=sub_payload, timeout=300)
            if sr.status_code == 200:
                group["subs"] = (sr.json() or {}).get("documents") or []
        groups.append(group)

    return groups


def lookup_document(
    *,
    token: str,
    base_url: str,
    document_number: str,
    case_number: str | None = None,
) -> dict | None:
    """Look up a single Nova document by documentNumber (within an optional case)."""
    url = f"{base_url}/Document/GetList?api-version=2.0-Case"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "common": {"transactionId": str(uuid.uuid4())},
        "paging": {"startRow": 1, "numberOfRows": 100},
        "documentNumber": document_number,
        "getOutput": {
            "documentDate": True,
            "title": True,
            "fileExtension": True,
            "documentType": True,
        },
    }
    if case_number:
        payload["caseNumber"] = case_number

    r = nova_request("PUT", url, headers=headers, json=payload, timeout=120)
    r.raise_for_status()
    docs = (r.json() or {}).get("documents") or []
    return docs[0] if docs else None


def download_file(
    *,
    token: str,
    base_url: str,
    document_uuid: str,
    local_path: str,
    timeout: int = 300,
) -> None:
    """Download a single Nova document and write it to ``local_path``."""
    url = f"{base_url}/Document/GetFile?api-version=2.0-Case"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"common": {"transactionId": str(uuid.uuid4()), "uuid": document_uuid}}
    r = nova_request("PUT", url, headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    with open(local_path, "wb") as fh:
        fh.write(r.content)
