#!/usr/bin/env python3
"""Back up and restore MinIO objects plus Redis namespaces.

PostgreSQL is intentionally handled by pg_dump in the shell wrapper. This
module keeps binary/object and Redis serialization in typed client APIs.
"""

import argparse
import base64
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse


def _minio_config() -> tuple[str, str, str, str]:
    return (
        os.environ.get("FISHAGENT_MINIO_ENDPOINT", "http://127.0.0.1:9000"),
        os.environ.get("FISHAGENT_MINIO_ACCESS_KEY", "fishagent"),
        os.environ.get("FISHAGENT_MINIO_SECRET_KEY", "fishagent-secret"),
        os.environ.get("FISHAGENT_MINIO_BUCKET", "fishagent-evidence"),
    )


def _redis_urls() -> list[str]:
    candidates = [
        os.environ.get("FISHAGENT_REDIS_URL", "redis://127.0.0.1:6379/0"),
        os.environ.get("FISHAGENT_CELERY_BROKER_URL", "redis://127.0.0.1:6379/1"),
        os.environ.get("FISHAGENT_CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/2"),
    ]
    return list(dict.fromkeys(url for url in candidates if url))


def _safe_object_path(root: Path, object_name: str) -> Path:
    relative = PurePosixPath(object_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe object name: %s" % object_name)
    path = root.joinpath(*relative.parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def backup_minio(destination: Path) -> dict[str, Any]:
    from minio import Minio

    endpoint, access_key, secret_key, bucket = _minio_config()
    parsed = urlparse(endpoint)
    client = Minio(parsed.netloc or parsed.path, access_key=access_key, secret_key=secret_key, secure=parsed.scheme == "https")
    destination.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    for item in client.list_objects(bucket, recursive=True):
        object_name = item.object_name
        response = client.get_object(bucket, object_name)
        try:
            data = response.read()
        finally:
            response.close()
            response.release_conn()
        path = _safe_object_path(destination / "objects", object_name)
        path.write_bytes(data)
        manifest.append(
            {
                "object_name": object_name,
                "content_type": (getattr(item, "metadata", None) or {}).get("content-type", "application/octet-stream"),
                "size": len(data),
                "etag": item.etag,
            }
        )
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"bucket": bucket, "objects": len(manifest)}


def restore_minio(source: Path) -> dict[str, Any]:
    from minio import Minio
    from minio.error import S3Error

    endpoint, access_key, secret_key, bucket = _minio_config()
    parsed = urlparse(endpoint)
    client = Minio(parsed.netloc or parsed.path, access_key=access_key, secret_key=secret_key, secure=parsed.scheme == "https")
    try:
        client.make_bucket(bucket)
    except S3Error as exc:
        if exc.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            raise
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    restored = 0
    for item in manifest:
        object_name = str(item["object_name"])
        path = _safe_object_path(source / "objects", object_name)
        with path.open("rb") as handle:
            client.put_object(
                bucket,
                object_name,
                handle,
                length=path.stat().st_size,
                content_type=str(item.get("content_type") or "application/octet-stream"),
            )
        restored += 1
    return {"bucket": bucket, "objects": restored}


def _redis_client(url: str):
    import redis

    return redis.Redis.from_url(url, decode_responses=False)


def backup_redis(destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True)
    result: list[dict[str, Any]] = []
    for index, url in enumerate(_redis_urls()):
        client = _redis_client(url)
        entries = []
        for key in client.scan_iter():
            value = client.dump(key)
            if value is None:
                continue
            entries.append(
                {
                    "key": base64.b64encode(key).decode("ascii"),
                    "value": base64.b64encode(value).decode("ascii"),
                    "ttl_ms": client.pttl(key),
                }
            )
        (destination / ("db-%d.json" % index)).write_text(json.dumps(entries), encoding="utf-8")
        result.append({"url": url.rsplit("/", 1)[0] + "/%s" % url.rsplit("/", 1)[-1], "keys": len(entries)})
    return {"databases": result}


def restore_redis(source: Path) -> dict[str, Any]:
    restored = []
    for index, url in enumerate(_redis_urls()):
        path = source / ("db-%d.json" % index)
        if not path.exists():
            continue
        client = _redis_client(url)
        client.flushdb()
        entries = json.loads(path.read_text(encoding="utf-8"))
        for entry in entries:
            ttl_ms = int(entry.get("ttl_ms", -1))
            if ttl_ms == 0:
                continue
            client.restore(
                base64.b64decode(entry["key"]),
                max(ttl_ms, 0),
                base64.b64decode(entry["value"]),
                replace=True,
            )
        restored.append({"db": index, "keys": len(entries)})
    return {"databases": restored}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["backup", "restore"])
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    if args.mode == "backup":
        result = {"minio": backup_minio(args.directory / "minio"), "redis": backup_redis(args.directory / "redis")}
    else:
        result = {"minio": restore_minio(args.directory / "minio"), "redis": restore_redis(args.directory / "redis")}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
