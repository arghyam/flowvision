"""
Keeps every photo sent to the service, unchanged, for research and for checking readings later.

Photos are named by request id in a folder per day (<YYYY-MM-DD>/<request id>.<ext>), so a name holds no consumer
details; the reading's database row links the two and stores this relative name. Two stores:
- LocalImageStore: a folder (a persistent disk on EC2; containers lose it on redeploy);
- S3ImageStore: s3://bucket/prefix (use this on AWS; credentials come from the instance or task IAM role).
"""
import os
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from service.vision.images import sniff_mime

EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MIME_TYPES = {ext: mime for mime, ext in EXTENSIONS.items()}
# The only names this service writes; anything else is refused on read (no path tricks).
_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(jpg|png|webp|bin)$")


def _name(request_id, image_bytes, when: datetime) -> str:
    ext = EXTENSIONS.get(sniff_mime(image_bytes), "bin")
    return f"{when.strftime('%Y-%m-%d')}/{request_id}.{ext}"


def media_type(name: str) -> str:
    return MIME_TYPES.get(name.rsplit(".", 1)[-1], "application/octet-stream")


class LocalImageStore:
    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def describe(self) -> str:
        return str(self.root)

    def save(self, request_id, image_bytes: bytes, when: datetime) -> str:
        """Writes the photo and returns its name relative to the store (e.g. '2026-09-30/<id>.jpg')."""
        name = _name(request_id, image_bytes, when)
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{request_id}.{uuid4().hex}.part")     # unique per request and write
        tmp.write_bytes(image_bytes)
        os.replace(tmp, target)
        return name

    def load(self, name: str) -> bytes | None:
        if not name or not _NAME.match(name):
            return None
        path = (self.root / name).resolve()
        return path.read_bytes() if path.is_relative_to(self.root) and path.is_file() else None


class S3ImageStore:
    def __init__(self, uri: str, client=None):
        bucket, _, prefix = uri[len("s3://"):].partition("/")
        if not bucket:
            raise ValueError(f"not an S3 location: {uri!r} (use s3://bucket/prefix)")
        self.bucket, self.prefix = bucket, prefix.strip("/")
        if client is None:
            import boto3                                   # only needed when photos go to S3
            from botocore.config import Config as BotoConfig
            client = boto3.client("s3", config=BotoConfig(retries={"max_attempts": 2, "mode": "standard"},
                                                          connect_timeout=3, read_timeout=10))
        self.client = client

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def save(self, request_id, image_bytes: bytes, when: datetime) -> str:
        name = _name(request_id, image_bytes, when)
        self.client.put_object(Bucket=self.bucket, Key=self._key(name), Body=image_bytes,
                               ContentType=media_type(name), ServerSideEncryption="AES256")
        return name

    def load(self, name: str) -> bytes | None:
        if not name or not _NAME.match(name):
            return None
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self._key(name))["Body"].read()
        except self.client.exceptions.NoSuchKey:
            return None
        except Exception as e:  # noqa: BLE001 - without s3:ListBucket a missing (e.g. expired) photo is AccessDenied
            code = getattr(e, "response", {}).get("Error", {}).get("Code")
            if code in ("AccessDenied", "NoSuchKey", "404", "NotFound"):
                return None
            raise


def make_image_store(location: str | None):
    """The store for a location from conf.config.image_location(): s3://..., a folder, or None (photos not kept)."""
    if not location:
        return None
    return S3ImageStore(location) if location.lower().startswith("s3://") else LocalImageStore(location)
