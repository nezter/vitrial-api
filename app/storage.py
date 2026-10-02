from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.settings import settings


class StorageError(Exception):
    """Evidence object storage is unavailable or returned an unexpected result."""


class StorageDigestMismatch(StorageError):
    """Streamed bytes did not match the caller-provided SHA-256 digest."""


@dataclass(frozen=True)
class StoredObject:
    sha256: str
    size_bytes: int


def object_key_for(organization_id: str, document_id: str, sha256: str) -> str:
    org = hashlib.sha256(organization_id.encode()).hexdigest()
    document = hashlib.sha256(document_id.encode()).hexdigest()
    digest = sha256.lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("invalid SHA-256 digest")
    return f"blobs/{org}/{document}/{digest}"


class ObjectStore:
    provider: str

    async def put_verified(
        self,
        key: str,
        chunks: AsyncIterable[bytes],
        expected_sha256: str,
    ) -> StoredObject:
        raise NotImplementedError

    async def exists(self, key: str) -> bool:
        raise NotImplementedError

    async def stream(self, key: str) -> AsyncIterator[bytes]:
        raise NotImplementedError
        yield b""  # pragma: no cover

    async def delete(self, key: str) -> None:
        raise NotImplementedError


class LocalObjectStore(ObjectStore):
    provider = "local"

    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        root = self.root.resolve()
        raw = Path(key)
        if raw.is_absolute():
            candidate = raw.resolve()
        else:
            # Legacy rows stored str(EVIDENCE_ROOT / org / hash). If the key already resolves
            # beneath root, use it directly; new content-addressed keys are relative to root.
            direct = raw.resolve()
            candidate = direct if direct.is_relative_to(root) else (root / raw).resolve()
        if not candidate.is_relative_to(root):
            raise StorageError("local object key escapes evidence root")
        return candidate

    async def put_verified(
        self,
        key: str,
        chunks: AsyncIterable[bytes],
        expected_sha256: str,
    ) -> StoredObject:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_dir = self.root / ".tmp"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        temporary = temporary_dir / uuid4().hex
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("wb") as handle:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    data = bytes(chunk)
                    handle.write(data)
                    digest.update(data)
                    size += len(data)
            actual = digest.hexdigest()
            if actual.lower() != expected_sha256.lower():
                raise StorageDigestMismatch("content digest mismatch")
            temporary.replace(destination)
            return StoredObject(actual, size)
        finally:
            temporary.unlink(missing_ok=True)

    async def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    async def stream(self, key: str) -> AsyncIterator[bytes]:
        path = self._path(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        with path.open("rb") as handle:
            while True:
                chunk = await asyncio.to_thread(handle.read, 1024 * 1024)
                if not chunk:
                    break
                yield chunk

    async def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3ObjectStore(ObjectStore):
    provider = "s3"

    def __init__(self):
        if not settings.s3_bucket:
            raise StorageError("S3_BUCKET is required")
        self.bucket = settings.s3_bucket
        self.part_size = max(settings.s3_part_size_bytes, 5 * 1024 * 1024)
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name=settings.s3_region,
            config=Config(s3={"addressing_style": "path"}),
        )

    async def put_verified(
        self,
        key: str,
        chunks: AsyncIterable[bytes],
        expected_sha256: str,
    ) -> StoredObject:
        staging = f".staging/{uuid4().hex}"
        digest = hashlib.sha256()
        size = 0
        upload_id: str | None = None
        parts: list[dict] = []
        buffer = bytearray()
        try:
            created = await asyncio.to_thread(
                self.client.create_multipart_upload,
                Bucket=self.bucket,
                Key=staging,
                ContentType="application/octet-stream",
            )
            upload_id = created["UploadId"]
            part_number = 1

            async def upload_part(data: bytes) -> None:
                nonlocal part_number
                response = await asyncio.to_thread(
                    self.client.upload_part,
                    Bucket=self.bucket,
                    Key=staging,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=data,
                )
                parts.append({"PartNumber": part_number, "ETag": response["ETag"]})
                part_number += 1

            async for chunk in chunks:
                if not chunk:
                    continue
                data = bytes(chunk)
                digest.update(data)
                size += len(data)
                buffer.extend(data)
                while len(buffer) >= self.part_size:
                    part = bytes(buffer[: self.part_size])
                    del buffer[: self.part_size]
                    await upload_part(part)

            if buffer or not parts:
                await upload_part(bytes(buffer))

            await asyncio.to_thread(
                self.client.complete_multipart_upload,
                Bucket=self.bucket,
                Key=staging,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
            upload_id = None

            actual = digest.hexdigest()
            if actual.lower() != expected_sha256.lower():
                raise StorageDigestMismatch("content digest mismatch")

            await asyncio.to_thread(
                self.client.copy_object,
                Bucket=self.bucket,
                Key=key,
                CopySource={"Bucket": self.bucket, "Key": staging},
                MetadataDirective="COPY",
            )
            await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=staging)
            return StoredObject(actual, size)
        except StorageDigestMismatch:
            if upload_id is not None:
                await asyncio.to_thread(
                    self.client.abort_multipart_upload,
                    Bucket=self.bucket,
                    Key=staging,
                    UploadId=upload_id,
                )
            else:
                await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=staging)
            raise
        except Exception as exc:
            if upload_id is not None:
                try:
                    await asyncio.to_thread(
                        self.client.abort_multipart_upload,
                        Bucket=self.bucket,
                        Key=staging,
                        UploadId=upload_id,
                    )
                except Exception:
                    pass
            else:
                try:
                    await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=staging)
                except Exception:
                    pass
            raise StorageError(str(exc)) from exc

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = exc.response.get("Error", {}).get("Code")
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise StorageError(str(exc)) from exc

    async def stream(self, key: str) -> AsyncIterator[bytes]:
        try:
            response = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404:
                raise FileNotFoundError(key) from exc
            raise StorageError(str(exc)) from exc
        body = response["Body"]
        try:
            while True:
                chunk = await asyncio.to_thread(body.read, 1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(body.close)

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise StorageError(str(exc)) from exc


_LOCAL_STORE = LocalObjectStore(settings.evidence_root)
_S3_STORE: S3ObjectStore | None = None


def store_for_provider(provider: str) -> ObjectStore:
    global _S3_STORE
    if provider == "local":
        return _LOCAL_STORE
    if provider == "s3":
        if _S3_STORE is None:
            _S3_STORE = S3ObjectStore()
        return _S3_STORE
    raise StorageError(f"unsupported evidence storage provider: {provider}")


def current_store() -> ObjectStore:
    return store_for_provider(settings.evidence_storage_provider)
