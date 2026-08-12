"""S3-compatible evidence storage backed by MinIO in local environments."""

import io
import uuid
from typing import Optional
from urllib.parse import urlparse
from urllib.request import urlopen


class MinioObjectStore:
    def __init__(self, endpoint: str, access_key: str = "", secret_key: str = "", bucket: str = "fishagent-evidence") -> None:
        self.endpoint = endpoint.rstrip("/")
        parsed = urlparse(self.endpoint)
        self.host = parsed.netloc or parsed.path
        self.secure = parsed.scheme == "https"
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self._client = None
        self.last_error: Optional[str] = None

    def _get_client(self):
        if self._client is None:
            try:
                from minio import Minio

                self._client = Minio(self.host, access_key=self.access_key, secret_key=self.secret_key, secure=self.secure)
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("minio package is required when MinIO is configured") from exc
        return self._client

    def health(self) -> dict:
        try:
            with urlopen(self.endpoint + "/minio/health/live", timeout=2) as response:
                if not 200 <= response.status < 300:
                    raise OSError("MinIO health status %s" % response.status)
            client = self._get_client()
            if not client.bucket_exists(self.bucket):
                client.make_bucket(self.bucket)
            self.last_error = None
            return {"status": "ok", "backend": "minio", "endpoint": self.endpoint, "bucket": self.bucket}
        except Exception as exc:  # health must report degraded instead of taking down readiness
            self.last_error = str(exc)
            return {"status": "degraded", "backend": "minio", "endpoint": self.endpoint, "bucket": self.bucket}

    def put_bytes(self, data: bytes, content_type: str, prefix: str = "evidence") -> dict:
        object_name = "%s/%s" % (prefix.strip("/"), uuid.uuid4().hex)
        client = self._get_client()
        result = client.put_object(
            self.bucket,
            object_name,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type or "application/octet-stream",
        )
        return {"bucket": self.bucket, "object_name": object_name, "etag": result.etag}

    def presigned_get(self, object_name: str, expires_seconds: int = 900) -> str:
        from datetime import timedelta

        return self._get_client().presigned_get_object(self.bucket, object_name, expires=timedelta(seconds=expires_seconds))


def object_store_from_config(
    endpoint: str,
    access_key: str = "",
    secret_key: str = "",
    bucket: str = "fishagent-evidence",
) -> Optional[MinioObjectStore]:
    return MinioObjectStore(endpoint, access_key, secret_key, bucket) if endpoint.strip() else None
