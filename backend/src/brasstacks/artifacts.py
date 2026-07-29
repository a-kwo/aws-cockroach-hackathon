"""Where the Maker's deliverables live.

The artifact text is the thing of value; S3 is where a durable copy goes so the
owner can open it later from any device. Keeping the store behind a narrow
protocol means the Maker is testable offline, same as every other boundary in
this codebase.

Deliberately not a general file abstraction. It does one thing — put a text
document somewhere addressable — because that is all the product needs and a
wider interface would invite the Maker to grow responsibilities it should not
have.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

DEFAULT_CONTENT_TYPE = "text/markdown; charset=utf-8"


class ArtifactStoreError(RuntimeError):
    """Storing an artifact failed. Names the key so the failure is locatable."""


@dataclass(frozen=True)
class StoredLocation:
    bucket: str
    key: str


@runtime_checkable
class ArtifactStore(Protocol):
    def put(self, *, key: str, body: str,
            content_type: str = DEFAULT_CONTENT_TYPE) -> StoredLocation:
        """Write `body` at `key` and return where it landed."""
        ...


class S3ArtifactStore:
    """Amazon S3, via boto3."""

    def __init__(self, *, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def put(self, *, key: str, body: str,
            content_type: str = DEFAULT_CONTENT_TYPE) -> StoredLocation:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                # Encode explicitly: the drafts contain the owner's own
                # punctuation, and boto3's default handling of str bodies is
                # not somewhere to leave an encoding to chance.
                Body=body.encode("utf-8"),
                ContentType=content_type,
            )
        except Exception as e:
            raise ArtifactStoreError(
                f"could not store artifact at {key!r} in {self._bucket!r} "
                f"({type(e).__name__}: {e})"
            ) from e
        return StoredLocation(bucket=self._bucket, key=key)


class FakeArtifactStore:
    """Offline stand-in. Records what it was asked to store.

    `fail=True` simulates an unavailable bucket, which is the case the Maker has
    to survive without losing the draft.
    """

    def __init__(self, *, bucket: str = "fake-artifacts",
                 fail: bool = False) -> None:
        self._bucket = bucket
        self._fail = fail
        self.puts: list[dict[str, Any]] = []

    def put(self, *, key: str, body: str,
            content_type: str = DEFAULT_CONTENT_TYPE) -> StoredLocation:
        if self._fail:
            raise ArtifactStoreError(f"could not store artifact at {key!r}")
        self.puts.append({"key": key, "body": body,
                          "content_type": content_type})
        return StoredLocation(bucket=self._bucket, key=key)


def build_artifact_store(settings: Any) -> ArtifactStore:
    """Real S3 store from settings. boto3 imported lazily, as elsewhere."""
    import boto3

    return S3ArtifactStore(
        client=boto3.client("s3", region_name=settings.aws_region),
        bucket=settings.s3_artifact_bucket,
    )
