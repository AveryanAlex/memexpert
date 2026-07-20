"""Unit coverage for bounded chronological recommendation evaluation."""

from __future__ import annotations

import json
import tomllib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self, cast

import pytest

from memexpert.core.config import Settings
from memexpert.services.recommendations.offline_evaluator import (
    HARD_MAX_CATALOG,
    OfflineCatalogItem,
    OfflineEvaluationBounds,
    OfflineEvaluationPolicy,
    OfflineRetrievalVariant,
    RecommendationObservation,
    UserPositiveHistory,
    build_chronological_cases,
    evaluate_chronological_retrieval,
)
from scripts import recommendation_evaluator as cli

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _id(value: int) -> uuid.UUID:
    return uuid.UUID(int=value)


def test_chronological_cases_use_only_strictly_earlier_available_distinct_items() -> None:
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    catalog = [
        OfflineCatalogItem(_id(1), (1.0, 0.0), started_at),
        OfflineCatalogItem(_id(2), (0.0, 1.0), started_at),
        OfflineCatalogItem(_id(3), (1.0, 0.0), started_at),
        # This observation is invalid for replay because the item was not in
        # the catalog at the time of the action.
        OfflineCatalogItem(_id(4), (0.0, -1.0), started_at + timedelta(days=10)),
        OfflineCatalogItem(_id(5), (1.0, 1.0), started_at),
    ]
    history = UserPositiveHistory(
        user_id=_id(100),
        observations=(
            RecommendationObservation(_id(1), started_at + timedelta(days=1), 4.0),
            RecommendationObservation(_id(2), started_at + timedelta(days=2), 4.0),
            RecommendationObservation(_id(3), started_at + timedelta(days=3), 4.0),
            RecommendationObservation(_id(4), started_at + timedelta(days=4), 4.0),
            RecommendationObservation(_id(5), started_at + timedelta(days=5), 5.0),
        ),
    )

    cases = build_chronological_cases(
        catalog=catalog,
        histories=[history],
        max_users=1,
        max_cases=1,
    )

    assert len(cases) == 1
    case = cases[0]
    assert case.target_meme_id == _id(5)
    assert [item.meme_id for item in case.history] == [_id(1), _id(2), _id(3)]
    assert all(item.occurred_at < case.cutoff_at for item in case.history)
    assert case.target_meme_id not in {item.meme_id for item in case.history}


def test_chronological_cases_train_on_weak_signals_but_hold_out_only_strong_targets() -> None:
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    catalog = [
        OfflineCatalogItem(_id(1), (1.0, 0.0), started_at),
        OfflineCatalogItem(_id(2), (0.0, 1.0), started_at),
        OfflineCatalogItem(_id(3), (1.0, 1.0), started_at),
    ]
    history = UserPositiveHistory(
        user_id=_id(100),
        observations=(
            RecommendationObservation(
                _id(1),
                started_at + timedelta(hours=1),
                1.0,
                is_strong_positive=False,
            ),
            # A weaker precursor for the held-out meme must not leak into its
            # training history.
            RecommendationObservation(
                _id(3),
                started_at + timedelta(hours=2),
                2.0,
                is_strong_positive=False,
            ),
            RecommendationObservation(
                _id(2),
                started_at + timedelta(hours=3),
                2.0,
                is_strong_positive=False,
            ),
            RecommendationObservation(
                _id(3),
                started_at + timedelta(hours=4),
                4.0,
                is_strong_positive=True,
            ),
        ),
    )

    cases = build_chronological_cases(
        catalog=catalog,
        histories=[history],
        max_users=1,
        max_cases=10,
    )

    assert len(cases) == 1
    assert cases[0].target_meme_id == _id(3)
    assert [item.meme_id for item in cases[0].history] == [_id(1), _id(2)]
    assert all(not item.is_strong_positive for item in cases[0].history)


def test_all_variants_report_expected_aggregate_metrics_without_user_identity() -> None:
    started_at = datetime(2026, 2, 1, tzinfo=UTC)
    source_id = _id(800)
    template_id = _id(900)
    catalog = [
        OfflineCatalogItem(_id(1), (1.0, 0.0), started_at, source_id, template_id),
        OfflineCatalogItem(_id(2), (0.0, 1.0), started_at, source_id, template_id),
        OfflineCatalogItem(_id(3), (1.0, 0.0), started_at, source_id, template_id),
        OfflineCatalogItem(_id(4), (-1.0, 0.0), started_at, _id(801), _id(901)),
    ]
    private_user_id = _id(0xABCDEF)
    histories = [
        UserPositiveHistory(
            user_id=private_user_id,
            observations=(
                RecommendationObservation(_id(1), started_at + timedelta(hours=1), 4.0),
                RecommendationObservation(_id(2), started_at + timedelta(hours=2), 4.0),
                RecommendationObservation(_id(3), started_at + timedelta(hours=3), 4.0),
            ),
        )
    ]
    report = evaluate_chronological_retrieval(
        catalog=catalog,
        histories=histories,
        bounds=OfflineEvaluationBounds(max_users=1, max_catalog=4, max_cases=1, k=1),
        policy=OfflineEvaluationPolicy(cluster_activation_signals=2, cluster_min_items=1),
        generated_at=started_at,
    )

    assert report.cases == 1
    assert set(report.variants) == set(OfflineRetrievalVariant)
    for summary in report.variants.values():
        assert summary.cases == 1
        assert summary.recall_at_k == 1.0
        assert summary.ndcg_at_k == 1.0
        assert summary.catalog_coverage == 0.25
        assert summary.source_concentration == 1.0
        assert summary.template_concentration == 1.0
        assert summary.intra_list_diversity == 0.0

    serialized = json.dumps(report.to_dict(), sort_keys=True)
    assert str(private_user_id) not in serialized
    assert "query" not in serialized
    variants_payload = cast("dict[str, object]", report.to_dict()["variants"])
    assert set(variants_payload) == {variant.value for variant in OfflineRetrievalVariant}


def test_cli_parser_and_console_script_enforce_bounded_inputs() -> None:
    args = cli.build_parser().parse_args(
        ["--max-users", "3", "--max-catalog", "100", "--max-cases", "5", "--k", "10"]
    )
    assert (args.max_users, args.max_catalog, args.max_cases, args.k) == (3, 100, 5, 10)
    with pytest.raises(SystemExit):
        _ = cli.build_parser().parse_args(["--max-catalog", str(HARD_MAX_CATALOG + 1)])
    with pytest.raises(ValueError, match="k must not exceed max_catalog"):
        _ = OfflineEvaluationBounds(max_users=1, max_catalog=3, max_cases=1, k=4)

    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    assert (
        pyproject["project"]["scripts"]["memexpert-recommendation-evaluator"]
        == "scripts.recommendation_evaluator:main"
    )


async def test_cli_forces_read_only_transaction_rolls_back_and_prints_aggregate_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeEngine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    class FakeSession:
        statements: list[str]
        rolled_back: bool

        def __init__(self) -> None:
            self.statements = []
            self.rolled_back = False

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, statement: object) -> None:
            self.statements.append(str(statement))

        async def rollback(self) -> None:
            self.rolled_back = True

    class FakeReport:
        def to_dict(self) -> dict[str, object]:
            return {"schema_version": 1, "mode": "chronological_read_only", "sample": {"cases": 0}}

    engine = FakeEngine()
    session = FakeSession()

    async def fake_evaluate(
        candidate_session: object,
        *,
        settings: Settings,
        bounds: OfflineEvaluationBounds,
    ) -> FakeReport:
        assert candidate_session is session
        assert settings.pipeline_voyage_output_dimensions > 0
        assert bounds.max_users == 1
        return FakeReport()

    monkeypatch.setattr(cli, "get_settings", Settings)
    monkeypatch.setattr(cli, "build_async_engine", lambda **_kwargs: engine)
    monkeypatch.setattr(cli, "build_async_session_factory", lambda _engine: lambda: session)
    monkeypatch.setattr(cli, "evaluate_postgres_recommendations", fake_evaluate)

    status = await cli.run(["--max-users", "1", "--max-catalog", "10", "--max-cases", "1", "--k", "1"])

    assert status == 0
    assert session.statements == ["SET TRANSACTION READ ONLY"]
    assert session.rolled_back is True
    assert engine.disposed is True
    output = capsys.readouterr().out
    assert json.loads(output)["mode"] == "chronological_read_only"
    assert "user_id" not in output


async def test_cli_failure_output_never_includes_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_settings() -> Settings:
        raise RuntimeError("postgresql://secret-user:secret-password@example.invalid/database")

    monkeypatch.setattr(cli, "get_settings", fail_settings)

    status = await cli.run(
        ["--max-users", "1", "--max-catalog", "10", "--max-cases", "1", "--k", "1"]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert captured.err == "recommendation evaluation failed: RuntimeError\n"
    assert "secret" not in captured.err
