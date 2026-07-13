"""Visibility and provenance policy shared by ingest, merge, and admin flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import distinct, exists, select

from memexpert.models.content import Meme, MemeFile, MemeSource
from memexpert.models.enums import IngestSourceKind, MemeVisibilityMode

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ApproximateMergeScope:
    """Candidate scope allowed for one approximate-deduplication lookup."""

    is_public: bool
    uploader_user_id: uuid.UUID | None = None

    @property
    def can_match(self) -> bool:
        """Return whether this scope can ever produce an automatic merge target."""

        return self.is_public or self.uploader_user_id is not None


def initial_visibility_for_source(source_kind: IngestSourceKind) -> bool:
    """Return the AUTO-mode effective visibility for a brand-new meme."""

    return source_kind is IngestSourceKind.PUBLIC_CRAWLER


def resolve_visibility_mode(modes: Iterable[MemeVisibilityMode]) -> MemeVisibilityMode:
    """Resolve conflicting canonical modes without weakening an explicit override."""

    resolved = MemeVisibilityMode.AUTO
    for mode in modes:
        if mode is MemeVisibilityMode.FORCE_PRIVATE:
            return mode
        if mode is MemeVisibilityMode.FORCE_PUBLIC:
            resolved = mode
    return resolved


async def refresh_effective_visibility(
    session: AsyncSession,
    meme: Meme,
    *,
    incoming_source_kind: IngestSourceKind | None = None,
) -> bool:
    """Recompute and materialize effective visibility from mode plus provenance."""

    if meme.visibility_mode is MemeVisibilityMode.FORCE_PRIVATE:
        meme.is_public = False
    elif (
        meme.visibility_mode is MemeVisibilityMode.FORCE_PUBLIC
        or incoming_source_kind is IngestSourceKind.PUBLIC_CRAWLER
    ):
        meme.is_public = True
    else:
        meme.is_public = await meme_has_public_crawler_source(session, meme.id)
    return meme.is_public


async def meme_has_public_crawler_source(session: AsyncSession, meme_id: uuid.UUID) -> bool:
    """Return whether any historical source attached to the meme came from a public crawler."""

    return bool(
        await session.scalar(
            select(
                exists().where(
                    MemeSource.file_id == MemeFile.id,
                    MemeFile.meme_id == meme_id,
                    MemeSource.source_kind == IngestSourceKind.PUBLIC_CRAWLER,
                )
            )
        )
    )


async def load_meme_uploader_user_ids(
    session: AsyncSession,
    meme_id: uuid.UUID,
) -> tuple[uuid.UUID, ...]:
    """Load the stable distinct uploader set for all files of a canonical meme."""

    result = await session.execute(
        select(distinct(MemeSource.uploader_user_id))
        .select_from(MemeSource)
        .join(MemeFile, MemeFile.id == MemeSource.file_id)
        .where(
            MemeFile.meme_id == meme_id,
            MemeSource.uploader_user_id.is_not(None),
        )
        .order_by(MemeSource.uploader_user_id)
    )
    return tuple(user_id for user_id in result.scalars().all() if user_id is not None)


async def load_approximate_merge_scope(
    session: AsyncSession,
    meme: Meme,
) -> ApproximateMergeScope:
    """Build the strict public or same-single-uploader approximate merge scope."""

    if meme.is_public:
        return ApproximateMergeScope(is_public=True)
    uploader_ids = await load_meme_uploader_user_ids(session, meme.id)
    return ApproximateMergeScope(
        is_public=False,
        uploader_user_id=uploader_ids[0] if len(uploader_ids) == 1 else None,
    )


def incoming_approximate_merge_scope(
    *,
    source_kind: IngestSourceKind,
    uploader_user_id: uuid.UUID | None,
) -> ApproximateMergeScope:
    """Build the pre-materialization scope for a newly inspected ingest request."""

    is_public = initial_visibility_for_source(source_kind)
    return ApproximateMergeScope(
        is_public=is_public,
        uploader_user_id=None if is_public else uploader_user_id,
    )


__all__ = [
    "ApproximateMergeScope",
    "incoming_approximate_merge_scope",
    "initial_visibility_for_source",
    "load_approximate_merge_scope",
    "load_meme_uploader_user_ids",
    "meme_has_public_crawler_source",
    "refresh_effective_visibility",
    "resolve_visibility_mode",
]
