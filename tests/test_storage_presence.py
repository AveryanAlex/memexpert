"""Object-storage presence and missing-download classification tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from botocore.exceptions import ClientError

from memexpert.core.storage import (
    StorageObjectMissingError,
    StorageObjectPresence,
    check_object_presence,
    download_object_bytes,
    is_missing_storage_object_error,
)


def _client_error(code: str, *, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "provider diagnostic must not drive classification"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "HeadObject",
    )


@dataclass(slots=True)
class _StorageClient:
    head_error: Exception | None = None
    get_error: Exception | None = None

    def head_object(self, *, Bucket: str, Key: str) -> object:
        _ = (Bucket, Key)
        if self.head_error is not None:
            raise self.head_error
        return {"ContentLength": 1}

    def get_object(self, *, Bucket: str, Key: str) -> object:
        _ = (Bucket, Key)
        if self.get_error is not None:
            raise self.get_error
        return {"Body": _StorageBody()}


class _StorageBody:
    def read(self) -> bytes:
        return b"x"

    def close(self) -> None:
        return None


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_missing_object_classifier_accepts_only_definitive_s3_codes(code: str) -> None:
    assert is_missing_storage_object_error(_client_error(code, status=404)) is True


@pytest.mark.parametrize(
    "error",
    [
        _client_error("AccessDenied", status=403),
        _client_error("InternalError", status=500),
        RuntimeError("endpoint unavailable"),
    ],
)
def test_missing_object_classifier_does_not_conflate_outages_or_permissions(error: Exception) -> None:
    assert is_missing_storage_object_error(error) is False


@pytest.mark.asyncio
async def test_object_presence_is_tri_state() -> None:
    present = await check_object_presence(_StorageClient(), bucket="media", key="present")
    missing = await check_object_presence(
        _StorageClient(head_error=_client_error("NoSuchKey", status=404)),
        bucket="media",
        key="missing",
    )
    unavailable = await check_object_presence(
        _StorageClient(head_error=_client_error("AccessDenied", status=403)),
        bucket="media",
        key="private",
    )

    assert present is StorageObjectPresence.PRESENT
    assert missing is StorageObjectPresence.MISSING
    assert unavailable is StorageObjectPresence.UNAVAILABLE


@pytest.mark.asyncio
async def test_download_translates_only_definitive_absence_to_safe_missing_error() -> None:
    client = _StorageClient(get_error=_client_error("NoSuchKey", status=404))

    with pytest.raises(StorageObjectMissingError, match="no longer exists") as caught:
        await download_object_bytes(client, bucket="media", key="secret/original.webm")

    assert "secret/original.webm" not in str(caught.value)


@pytest.mark.asyncio
async def test_download_preserves_non_missing_storage_failure() -> None:
    error = _client_error("AccessDenied", status=403)
    client = _StorageClient(get_error=error)

    with pytest.raises(ClientError) as caught:
        await download_object_bytes(client, bucket="media", key="private/original.webm")

    assert caught.value is error
