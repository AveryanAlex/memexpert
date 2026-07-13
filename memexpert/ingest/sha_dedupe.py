"""Global exact-SHA serialization and canonical-file lookup helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select, text

from memexpert.models.content import MemeFile
from memexpert.models.enums import ContentProcessingStatus, SourceAttachReason

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def acquire_sha256_advisory_lock(session: AsyncSession, sha256_hex: str) -> None:
    """Serialize exact-hash decisions until the current PostgreSQL transaction ends."""

    unsigned_key = int.from_bytes(bytes.fromhex(sha256_hex)[:8], byteorder="big", signed=False)
    signed_key = unsigned_key if unsigned_key < 2**63 else unsigned_key - 2**64
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:sha256_lock_key)"),
        {"sha256_lock_key": signed_key},
    )


async def find_global_sha256_match(session: AsyncSession, sha256_hex: str) -> MemeFile | None:
    """Return the deterministic canonical file for a globally matching hash."""

    return await session.scalar(
        select(MemeFile)
        .where(MemeFile.sha256_hex == sha256_hex)
        .order_by(MemeFile.created_at.asc(), MemeFile.id.asc())
        .limit(1)
    )


def sha_match_attach_reason(matched_file: MemeFile) -> SourceAttachReason:
    """Classify an exact-file source attachment, including quarantined files."""

    if (
        matched_file.status is ContentProcessingStatus.FAILED
        and matched_file.blocked_perceptual_hash_id is not None
    ):
        return SourceAttachReason.BLOCKED_SHA256_EXISTING_FILE
    return SourceAttachReason.SHA256_EXACT_EXISTING_FILE


__all__ = [
    "acquire_sha256_advisory_lock",
    "find_global_sha256_match",
    "sha_match_attach_reason",
]
