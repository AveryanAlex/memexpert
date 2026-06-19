"""Derived public engagement read-model helpers."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


async def load_derived_popularity_scores(
    session: AsyncSession,
    meme_ids: tuple[uuid.UUID, ...],
) -> dict[uuid.UUID, float]:
    """Return read-model popularity scores for the requested memes.

    Scores are not canonical storage. They are derived from the latest successful
    source engagement snapshot per source post plus cumulative platform analytics
    events. Missing/unknown source counters remain NULL in snapshots and are only
    coalesced here for public ranking/index payloads.
    """

    unique_meme_ids = tuple(dict.fromkeys(meme_ids))
    if not unique_meme_ids:
        return {}

    result = await session.execute(
        _DERIVED_POPULARITY_SCORE_SQL,
        {"meme_ids": list(unique_meme_ids)},
    )
    return {
        cast("uuid.UUID", row["meme_id"]): float(row["popularity_score"] or 0.0)
        for row in result.mappings()
    }


async def load_derived_popularity_score(session: AsyncSession, meme_id: uuid.UUID) -> float:
    """Return one derived read-model popularity score, defaulting to 0."""

    return (await load_derived_popularity_scores(session, (meme_id,))).get(meme_id, 0.0)


_DERIVED_POPULARITY_SCORE_SQL = text(
    """
    WITH requested AS (
        SELECT unnest(CAST(:meme_ids AS uuid[])) AS meme_id
    ),
    safe_events AS (
        SELECT
            CASE
                WHEN jsonb_typeof(payload -> 'meme_id') = 'string'
                AND payload ->> 'meme_id' ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                THEN (payload ->> 'meme_id')::uuid
                WHEN jsonb_typeof(payload -> 'refs' -> 'meme_id') = 'string'
                AND payload -> 'refs' ->> 'meme_id' ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                THEN (payload -> 'refs' ->> 'meme_id')::uuid
                ELSE NULL
            END AS meme_id,
            event_type::text AS event_type
        FROM analytics_events
        WHERE event_type::text IN (
            'meme_view',
            'view',
            'meme_send',
            'share',
            'meme_share',
            'inline_sent',
            'meme_save',
            'save',
            'meme_like',
            'favorite'
        )
    ),
    event_totals AS (
        SELECT
            se.meme_id,
            count(*) FILTER (WHERE se.event_type IN ('meme_view', 'view'))::integer AS platform_views,
            count(*) FILTER (
                WHERE se.event_type IN ('meme_send', 'share', 'meme_share', 'inline_sent')
            )::integer AS platform_sends,
            count(*) FILTER (WHERE se.event_type IN ('meme_save', 'save'))::integer AS platform_saves,
            count(*) FILTER (WHERE se.event_type IN ('meme_like', 'favorite'))::integer AS platform_likes
        FROM safe_events se
        JOIN requested r ON r.meme_id = se.meme_id
        GROUP BY se.meme_id
    ),
    latest_source_snapshots AS (
        SELECT DISTINCT ON (ses.meme_source_id)
            mf.meme_id,
            ses.meme_source_id,
            ses.captured_at,
            ses.view_count,
            ses.reaction_count,
            ses.forward_count
        FROM meme_source_engagement_snapshots ses
        JOIN meme_sources ms ON ms.id = ses.meme_source_id
        JOIN meme_files mf ON mf.id = ms.file_id
        JOIN requested r ON r.meme_id = mf.meme_id
        WHERE ses.fetch_status::text = 'success'
        ORDER BY ses.meme_source_id, ses.captured_at DESC, ses.id DESC
    ),
    source_totals AS (
        SELECT
            meme_id,
            sum(COALESCE(view_count, 0))::integer AS source_views,
            sum(COALESCE(reaction_count, 0))::integer AS source_reactions,
            sum(COALESCE(forward_count, 0))::integer AS source_reposts
        FROM latest_source_snapshots
        GROUP BY meme_id
    )
    SELECT
        r.meme_id,
        (
            ln(1.0 + GREATEST(COALESCE(st.source_views, 0), 0)) * 1.0
            + ln(1.0 + GREATEST(COALESCE(st.source_reactions, 0), 0)) * 2.0
            + ln(1.0 + GREATEST(COALESCE(st.source_reposts, 0), 0)) * 3.0
            + ln(1.0 + GREATEST(COALESCE(et.platform_views, 0), 0)) * 1.0
            + ln(1.0 + GREATEST(COALESCE(et.platform_sends, 0), 0)) * 3.0
            + ln(1.0 + GREATEST(COALESCE(et.platform_saves, 0), 0)) * 4.0
            + ln(1.0 + GREATEST(COALESCE(et.platform_likes, 0), 0)) * 5.0
        )::double precision AS popularity_score
    FROM requested r
    LEFT JOIN source_totals st ON st.meme_id = r.meme_id
    LEFT JOIN event_totals et ON et.meme_id = r.meme_id
    """
).bindparams(bindparam("meme_ids", type_=ARRAY(PGUUID(as_uuid=True))))


__all__ = ["load_derived_popularity_score", "load_derived_popularity_scores"]
