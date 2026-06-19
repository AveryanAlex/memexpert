"""Temporary and canonical object movement for ingest materialization."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

from memexpert.core.config import get_settings
from memexpert.core.storage import (
    delete_object_if_present,
    download_object_bytes,
    get_pipeline_storage_settings,
    get_s3_client,
    upload_object_bytes,
)
from memexpert.services.errors import PipelineIngestError, PipelineStorageError

if TYPE_CHECKING:
    from memexpert.core.config import Settings
    from memexpert.ingest.materialization.models import ObjectStorageClient, PreparedMaterialization


class MaterializationObjectStore:
    """Object-storage boundary for raw temp reads and canonical original writes."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        storage_client: ObjectStorageClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._storage_settings = get_pipeline_storage_settings(self._settings)
        self._storage_client = storage_client or cast("ObjectStorageClient", get_s3_client())

    async def download_temp_original(self, key: str) -> bytes:
        """Read raw upload bytes from temporary object storage."""

        try:
            return await download_object_bytes(
                self._storage_client,
                bucket=self._storage_settings.bucket,
                key=key,
            )
        except Exception as exc:
            raise PipelineStorageError("Failed to download the raw original from temporary storage.") from exc

    async def put_canonical_original(self, *, prepared: PreparedMaterialization, media_bytes: bytes) -> None:
        """Promote inspected raw bytes into canonical original object storage."""

        try:
            await upload_object_bytes(
                self._storage_client,
                bucket=self._storage_settings.bucket,
                key=prepared.object_key,
                body=media_bytes,
                content_type=prepared.mime_type,
            )
        except Exception as exc:
            raise PipelineStorageError("Failed to promote the uploaded original into canonical storage.") from exc

    async def cleanup_temp_original(self, key: str) -> None:
        """Delete the temporary raw upload object if it still exists."""

        await delete_object_if_present(
            self._storage_client,
            bucket=self._storage_settings.bucket,
            key=key,
        )

    async def cleanup_canonical_original(self, key: str) -> None:
        """Delete a promoted canonical original after failed DB persistence."""

        await delete_object_if_present(
            self._storage_client,
            bucket=self._storage_settings.bucket,
            key=key,
        )


def meme_file_id_from_original_key(object_key: str) -> uuid.UUID:
    """Extract the meme-file UUID embedded in a canonical original object key."""

    try:
        return uuid.UUID(object_key.split("/")[-2])
    except (IndexError, ValueError) as exc:
        raise PipelineIngestError(f"Canonical object key {object_key!r} does not contain a meme_file_id.") from exc


__all__ = ["MaterializationObjectStore", "meme_file_id_from_original_key"]
