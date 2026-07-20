"""PostgreSQL contract coverage for the read-only offline evaluator loader."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from memexpert.core.config import Settings
from memexpert.models.collection import Collection, CollectionMeme
from memexpert.models.content import EmbeddingCache, Meme, MemeFile
from memexpert.models.enums import (
    AnalyticsEventType,
    CollectionKind,
    CollectionVisibility,
    ContentKind,
    ContentLanguage,
    ContentProcessingStatus,
    EmbeddingInputType,
)
from memexpert.models.user import AnalyticsEvent
from memexpert.services.recommendations.math import encode_vector
from memexpert.services.recommendations.offline_evaluator import (
    OfflineEvaluationBounds,
    OfflineRetrievalVariant,
    PostgresOfflineEvaluationLoader,
    evaluate_postgres_recommendations,
)
from tests.factories import build_full_user

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def test_postgres_offline_evaluator_loads_bounded_artifacts_without_writes(
    migrated_db_session: AsyncSession,
) -> None:
    observed_at = datetime(2026, 3, 1, tzinfo=UTC)
    user = build_full_user()
    migrated_db_session.add(user)
    await migrated_db_session.flush()

    meme_ids: list[uuid.UUID] = []
    for index, vector in enumerate(((1.0, 0.0), (0.0, 1.0), (1.0, 0.0), (-1.0, 0.0)), start=1):
        meme_id = uuid.uuid7()
        file_id = uuid.uuid7()
        meme_ids.append(meme_id)
        meme = Meme(
            id=meme_id,
            primary_file_id=file_id,
            media_type=ContentKind.IMAGE,
            language=ContentLanguage.EN,
            is_public=True,
            created_at=observed_at - timedelta(days=1),
        )
        migrated_db_session.add(meme)
        await migrated_db_session.flush()
        migrated_db_session.add_all(
            [
                MemeFile(
                    id=file_id,
                    meme_id=meme_id,
                    status=ContentProcessingStatus.READY,
                    s3_original_key=f"offline-evaluator/{meme_id}.jpg",
                ),
                EmbeddingCache(
                    input_hash=f"{index:064x}",
                    input_type=EmbeddingInputType.IMAGE,
                    embedding=encode_vector(vector),
                    model_version="offline-evaluator-test-model",
                    source_file_id=file_id,
                    created_at=observed_at,
                ),
            ]
        )
        await migrated_db_session.flush()

    mirrored_impression_id = "offline-evaluator-send"
    migrated_db_session.add_all(
        [
            AnalyticsEvent(
                user_id=user.id,
                event_type=AnalyticsEventType.MEME_DETAIL_CLICK,
                payload={"refs": {"meme_id": str(meme_ids[0])}},
                occurred_at=observed_at + timedelta(hours=1),
            ),
            AnalyticsEvent(
                user_id=user.id,
                event_type=AnalyticsEventType.MEME_ENGAGED_VIEW,
                payload={"refs": {"meme_id": str(meme_ids[1])}},
                occurred_at=observed_at + timedelta(hours=2),
            ),
            *(
                AnalyticsEvent(
                    user_id=user.id,
                    event_type=event_type,
                    payload={
                        "refs": {"meme_id": str(meme_ids[2])},
                        "impression_id": mirrored_impression_id,
                    },
                    occurred_at=observed_at + timedelta(hours=3, seconds=offset),
                )
                for offset, event_type in enumerate(
                    (
                        AnalyticsEventType.INLINE_CHOSEN,
                        AnalyticsEventType.INLINE_SENT,
                        AnalyticsEventType.MEME_SEND,
                    )
                )
            ),
        ]
    )
    saved_collection = Collection(
        owner_id=user.id,
        title="Offline evaluator state",
        kind=CollectionKind.CUSTOM,
        visibility=CollectionVisibility.PRIVATE,
    )
    migrated_db_session.add(saved_collection)
    await migrated_db_session.flush()
    migrated_db_session.add(
        CollectionMeme(
            collection_id=saved_collection.id,
            meme_id=meme_ids[3],
            added_by_user_id=user.id,
            added_at=observed_at + timedelta(hours=4),
        )
    )
    await migrated_db_session.commit()
    event_count_before = await migrated_db_session.scalar(select(func.count()).select_from(AnalyticsEvent))

    settings = Settings.model_validate(
        {
            "pipeline_voyage_model": "offline-evaluator-test-model",
            "pipeline_voyage_output_dimensions": 2,
        }
    )
    loader = PostgresOfflineEvaluationLoader(migrated_db_session, settings=settings)
    histories = await loader.load_histories(catalog_meme_ids=tuple(meme_ids), max_users=1)

    assert len(histories) == 1
    assert [(item.weight, item.is_strong_positive) for item in histories[0].observations] == [
        (1.0, False),
        (2.0, False),
        (4.0, True),
        (5.0, True),
    ]

    report = await evaluate_postgres_recommendations(
        migrated_db_session,
        settings=settings,
        bounds=OfflineEvaluationBounds(max_users=1, max_catalog=4, max_cases=2, k=1),
        generated_at=observed_at,
    )

    assert report.catalog_items == 4
    assert report.histories_loaded == 1
    assert report.observations_loaded == 4
    assert report.cases == 2
    assert set(report.variants) == set(OfflineRetrievalVariant)
    assert await migrated_db_session.scalar(select(func.count()).select_from(AnalyticsEvent)) == event_count_before
    assert str(user.id) not in json.dumps(report.to_dict(), sort_keys=True)
