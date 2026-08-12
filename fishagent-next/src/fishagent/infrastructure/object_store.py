from urllib.error import URLError
from urllib.request import urlopen
from typing import Optional


class MinioHealth:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint.rstrip("/")

    def health(self) -> dict:
        try:
            with urlopen(self.endpoint + "/minio/health/live", timeout=2) as response:
                if 200 <= response.status < 300:
                    return {"status": "ok", "backend": "minio", "endpoint": self.endpoint}
        except (OSError, URLError):
            pass
        return {"status": "degraded", "backend": "minio", "endpoint": self.endpoint}


def object_store_from_config(endpoint: str) -> Optional[MinioHealth]:
    return MinioHealth(endpoint) if endpoint.strip() else None
