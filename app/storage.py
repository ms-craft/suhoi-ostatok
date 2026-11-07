from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import Settings, get_settings


@dataclass(frozen=True)
class StoredObject:
    key: str
    modified_at: datetime
    etag: str
    size: int


class Storage(Protocol):
    def put(self, key: str, content: bytes, mime_type: str) -> None: ...
    def get(self, key: str) -> tuple[bytes, str]: ...
    def delete(self, key: str) -> None: ...
    def ping(self) -> None: ...


class MemoryStorage:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, content: bytes, mime_type: str) -> None:
        self.objects[key] = (content, mime_type)

    def get(self, key: str) -> tuple[bytes, str]:
        return self.objects[key]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def ping(self) -> None:
        pass


class S3Storage:
    def __init__(self, settings: Settings):
        self.bucket = settings.s3_bucket
        self.region = settings.s3_region
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=self.region,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=(
                settings.s3_secret_key.get_secret_value() if settings.s3_secret_key else None
            ),
            config=Config(
                connect_timeout=3,
                read_timeout=10,
                retries={"total_max_attempts": 2},
                s3={"addressing_style": "path"},
            ),
        )

    def put(self, key: str, content: bytes, mime_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=mime_type)

    def get(self, key: str) -> tuple[bytes, str]:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        with response["Body"] as stream:
            return stream.read(), response["ContentType"]

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def iter_objects(self, prefix: str) -> Iterator[StoredObject]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", ()):
                yield StoredObject(item["Key"], item["LastModified"], item["ETag"], item["Size"])

    def inspect_object(self, key: str) -> StoredObject | None:
        try:
            item = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise
        return StoredObject(key, item["LastModified"], item["ETag"], item["ContentLength"])

    def delete_unchanged(self, item: StoredObject) -> bool:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=item.key, IfMatch=item.etag)
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in {404, 412}:
                return False
            raise
        return True

    def ping(self) -> None:
        self.client.head_bucket(Bucket=self.bucket)

    def initialize(self) -> None:
        try:
            self.ping()
        except ClientError as error:
            if error.response["Error"]["Code"] not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            location = {} if self.region == "us-east-1" else {
                "CreateBucketConfiguration": {"LocationConstraint": self.region}
            }
            self.client.create_bucket(Bucket=self.bucket, **location)


def make_storage(settings: Settings) -> Storage:
    return MemoryStorage() if settings.storage_backend == "memory" else S3Storage(settings)


if __name__ == "__main__":
    S3Storage(get_settings()).initialize()
