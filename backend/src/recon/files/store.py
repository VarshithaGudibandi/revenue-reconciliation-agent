"""Reading files from an S3-compatible bucket, behind a small interface."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import boto3
from botocore.config import Config

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


class ObjectTooLargeError(ValueError):
    """The object is bigger than the limit."""


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    etag: str
    size: int


class ObjectStore(Protocol):
    bucket: str

    def list(self, prefix: str) -> Iterator[ObjectInfo]: ...

    def get(self, key: str, max_bytes: int) -> bytes: ...


class S3Store:
    """A bucket on S3 or anything that speaks its API (SeaweedFS, Garage, a test server)."""

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        self.bucket = bucket
        self._client: S3Client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            # Path-style addressing, because a local server has no wildcard DNS for buckets.
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
        )

    def list(self, prefix: str) -> Iterator[ObjectInfo]:
        pages = self._client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix
        )
        for page in pages:
            for item in page.get("Contents", []):
                yield ObjectInfo(item["Key"], item.get("ETag", "").strip('"'), item.get("Size", 0))

    def get(self, key: str, max_bytes: int) -> bytes:
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        if response["ContentLength"] > max_bytes:
            response["Body"].close()
            raise ObjectTooLargeError(
                f"{key} is {response['ContentLength']} bytes; the limit is {max_bytes}"
            )
        return response["Body"].read()
