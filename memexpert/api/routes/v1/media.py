# ruff: noqa: TC001,TC002,TC003
"""Authenticated media render/download redirects for authorized meme files."""

from __future__ import annotations

import uuid
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Path, status
from fastapi.responses import RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from memexpert.api.dependencies import CurrentUserDep, DbSessionDep
from memexpert.core.config import get_settings
from memexpert.core.storage import get_pipeline_storage_settings, get_s3_client
from memexpert.models.collection import Collection, CollectionMember, CollectionMeme
from memexpert.models.content import Meme, MemeFile
from memexpert.models.enums import AccountType

router = APIRouter(prefix="/media", tags=["media"])

_PRESIGNED_GET_TTL_SECONDS = 300


class MediaFileVariant(StrEnum):
    """Authenticated file variants emitted by private render DTOs."""

    THUMBNAIL = "thumbnail"
    PREVIEW = "preview"
    ORIGINAL = "original"
    DOWNLOAD = "download"
    WEB_VIDEO = "web-video.mp4"


@router.get("/files/{file_id}/{variant}", summary="Render an authorized meme file")
async def render_media_file(
    session: DbSessionDep,
    current_user: CurrentUserDep,
    file_id: Annotated[uuid.UUID, Path()],
    variant: Annotated[MediaFileVariant, Path()],
) -> RedirectResponse:
    """Redirect authenticated callers to a short-lived object URL for an authorized file variant."""

    file = await _load_authorized_file(
        session,
        file_id=file_id,
        user_id=current_user.id,
        allow_admin_access=current_user.is_admin and current_user.account_type is AccountType.FULL,
    )
    if file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file was not found.")

    object_key = file.s3_web_video_key if variant is MediaFileVariant.WEB_VIDEO else file.s3_original_key
    if object_key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media variant was not found.")

    settings = get_settings()
    storage_settings = get_pipeline_storage_settings(settings)
    params: dict[str, str] = {"Bucket": storage_settings.bucket, "Key": object_key}
    content_type = "video/mp4" if variant is MediaFileVariant.WEB_VIDEO else file.mime_type
    if content_type:
        params["ResponseContentType"] = content_type
    if variant is MediaFileVariant.DOWNLOAD:
        params["ResponseContentDisposition"] = f"attachment; filename*=UTF-8''{quote(_download_filename(file))}"

    url = get_s3_client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=_PRESIGNED_GET_TTL_SECONDS,
    )
    return RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


async def _load_authorized_file(
    session: AsyncSession,
    *,
    file_id: uuid.UUID,
    user_id: uuid.UUID,
    allow_admin_access: bool,
) -> MemeFile | None:
    authorized_collection = (
        select(CollectionMeme.meme_id)
        .select_from(CollectionMeme)
        .join(Collection, Collection.id == CollectionMeme.collection_id)
        .outerjoin(CollectionMember, CollectionMember.collection_id == Collection.id)
        .where(
            CollectionMeme.meme_id == Meme.id,
            or_(Collection.owner_id == user_id, CollectionMember.user_id == user_id),
        )
        .exists()
    )
    stmt = select(MemeFile).join(Meme, Meme.id == MemeFile.meme_id).where(MemeFile.id == file_id)
    if not allow_admin_access:
        stmt = stmt.where(
            or_(
                Meme.is_public.is_(True),
                Meme.author_user_id == user_id,
                authorized_collection,
            ),
        )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _download_filename(file: MemeFile) -> str:
    suffix = PurePosixPath(file.s3_original_key).suffix.lower()
    extension = suffix if suffix and len(suffix) <= 11 else ""
    return f"meme-file-{file.id}{extension}"


__all__ = ["router"]
