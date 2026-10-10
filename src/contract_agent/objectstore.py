"""Object storage for contract documents: the S3 port.

Contracts arrive as files (long PDFs, scans and their text layers);
the records store holds structured results, not documents. Documents
live behind this port, addressed by object keys
(``contracts/<contract_id>/<document_id>.txt``), never local paths:

- ``S3ObjectStore`` — any S3-compatible store (AWS S3, MinIO in the
  compose stack). A compact SigV4 client over the standard library:
  PUT / GET / HEAD, no SDK dependency. Configured with ``S3_BUCKET``,
  ``S3_ENDPOINT_URL`` (MinIO / compatible endpoints; empty = AWS),
  ``S3_ACCESS_KEY``, ``S3_SECRET_KEY``, ``S3_REGION``.
- ``InMemoryObjectStore`` — the test double for the hermetic suite
  and the offline demo; used when no bucket is configured.

The service archives every analysed contract's documents (and the
drafted packet) through this port when a store is configured, and the
record keeps the keys.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .config import env_str, load_dotenv


class InMemoryObjectStore:
    """Process-local object store — test double only."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}
        self._lock = threading.Lock()

    def put(self, key: str, data: bytes, content_type: str = "text/plain") -> str:
        with self._lock:
            self._objects[key] = (data, content_type)
        return key

    def get(self, key: str) -> bytes | None:
        with self._lock:
            entry = self._objects.get(key)
        return entry[0] if entry else None

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self._objects


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


class S3ObjectStore:
    """Minimal S3-compatible client (SigV4, path-style for endpoints,
    virtual-host style for AWS). Standard library only."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str = "",
        access_key: str = "",
        secret_key: str = "",
        region: str = "us-east-1",
    ) -> None:
        self._bucket = bucket
        self._endpoint = endpoint_url.rstrip("/")
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region or "us-east-1"

    # -- addressing -----------------------------------------------------

    def _url(self, key: str = "") -> str:
        if self._endpoint:
            # Path-style: <endpoint>/<bucket>/<key> (MinIO and friends).
            return f"{self._endpoint}/{self._bucket}/{key}" if key else f"{self._endpoint}/{self._bucket}"
        host = f"{self._bucket}.s3.{self._region}.amazonaws.com"
        return f"https://{host}/{key}" if key else f"https://{host}"

    def _host(self) -> str:
        if self._endpoint:
            return self._endpoint.split("://", 1)[-1]
        return f"{self._bucket}.s3.{self._region}.amazonaws.com"

    # -- SigV4 ----------------------------------------------------------

    def _request(self, method: str, key: str, data: bytes | None = None,
                 content_type: str = "text/plain") -> tuple[int, bytes]:
        url = self._url(key)
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(data or b"").hexdigest()
        headers = {
            "host": self._host(),
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if data is not None:
            headers["content-type"] = content_type
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
        canonical_uri = urllib.parse.urlsplit(url).path or "/"
        canonical_request = "\n".join(
            [method, canonical_uri, "", canonical_headers, signed_headers, payload_hash]
        )
        scope = f"{date_stamp}/{self._region}/s3/aws4_request"
        string_to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", amz_date, scope,
             hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
        )
        k_date = _sign(("AWS4" + self._secret_key).encode("utf-8"), date_stamp)
        k_region = _sign(k_date, self._region)
        k_service = _sign(k_region, "s3")
        k_signing = _sign(k_service, "aws4_request")
        signature = hmac.new(
            k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self._access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        request = urllib.request.Request(url, data=data, method=method)
        for name, value in headers.items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    # -- the port --------------------------------------------------------

    def put(self, key: str, data: bytes, content_type: str = "text/plain") -> str:
        status, body = self._request("PUT", key, data=data, content_type=content_type)
        if status not in (200, 201, 204):
            raise RuntimeError(
                f"object store PUT {key} failed with HTTP {status}: {body[:200]!r}"
            )
        return key

    def get(self, key: str) -> bytes | None:
        status, body = self._request("GET", key)
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(
                f"object store GET {key} failed with HTTP {status}: {body[:200]!r}"
            )
        return body

    def exists(self, key: str) -> bool:
        status, _ = self._request("HEAD", key)
        return status == 200

    def ensure_bucket(self) -> None:
        """Create the bucket when it does not exist (MinIO / first run)."""
        status, _ = self._request("HEAD", "")
        if status in (200, 301, 403):
            return
        status, body = self._request("PUT", "", data=b"")
        if status not in (200, 201):
            raise RuntimeError(
                f"object store bucket creation failed with HTTP {status}: {body[:200]!r}"
            )


def get_object_store() -> InMemoryObjectStore | S3ObjectStore:
    """The object store for this process: S3 when ``S3_BUCKET`` is set
    (compose and production), the in-memory double otherwise."""
    load_dotenv()
    bucket = env_str("S3_BUCKET", "")
    if bucket:
        return S3ObjectStore(
            bucket=bucket,
            endpoint_url=env_str("S3_ENDPOINT_URL", ""),
            access_key=env_str("S3_ACCESS_KEY", ""),
            secret_key=env_str("S3_SECRET_KEY", ""),
            region=env_str("S3_REGION", "us-east-1"),
        )
    return InMemoryObjectStore()
