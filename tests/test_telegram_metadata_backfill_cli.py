"""CLI contract for the Telegram post metadata backfill."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from memexpert.crawlers.telegram.metadata_backfill import TelegramPostMetadataBackfillResult
from scripts import backfill_telegram_post_metadata as cli

if TYPE_CHECKING:
    from collections.abc import Sequence


@pytest.mark.parametrize(
    "argv",
    [(), ("--dry-run", "--apply"), ("--apply", "--batch-size", "0"), ("--apply", "--batch-size", "101")],
)
def test_parser_requires_one_mode_and_bounded_batch_size(argv: Sequence[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.build_parser().parse_args(argv)
    assert exc_info.value.code == 2


def test_parser_accepts_repeatable_channel_filters() -> None:
    args = cli.build_parser().parse_args(
        ["--apply", "--channel", "first", "--channel", "@second", "--batch-size", "17"],
    )
    assert args.apply is True
    assert args.dry_run is False
    assert args.channel == ["first", "@second"]
    assert args.batch_size == 17


async def test_run_wires_apply_arguments_and_reports_retry_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: dict[str, object] = {}
    session_factory = object()
    settings = object()

    class _Backfiller:
        def __init__(self, actual_session_factory: object, *, settings: object) -> None:
            calls["init"] = (actual_session_factory, settings)

        async def run(
            self,
            *,
            dry_run: bool,
            channel_filters: Sequence[str],
            batch_size: int,
        ) -> TelegramPostMetadataBackfillResult:
            calls["run"] = (dry_run, tuple(channel_filters), batch_size)
            return TelegramPostMetadataBackfillResult(
                dry_run=dry_run,
                channels_inspected=2,
                batches_processed=3,
                candidates_inspected=4,
                captured=2,
                missing=1,
                transient_failures=1,
                permanent_failures=0,
                stale_candidates=0,
                unassigned_channels=0,
                sessions_parked=0,
                sessions_quarantined=0,
                sessions_skipped=0,
                sessions_requiring_attention=0,
            )

    monkeypatch.setattr(cli, "get_async_session_factory", lambda: session_factory)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "TelegramPostMetadataBackfiller", _Backfiller)

    exit_code = await cli.run(["--apply", "--channel", "one", "--channel", "two", "--batch-size", "2"])

    assert exit_code == 1
    assert calls == {
        "init": (session_factory, settings),
        "run": (False, ("one", "two"), 2),
    }
    assert "mode=apply" in capsys.readouterr().out


async def test_run_returns_operator_exit_for_permanent_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _Backfiller:
        def __init__(self, _session_factory: object, *, settings: object) -> None:
            _ = settings

        async def run(
            self,
            *,
            dry_run: bool,
            channel_filters: Sequence[str],
            batch_size: int,
        ) -> TelegramPostMetadataBackfillResult:
            _ = (channel_filters, batch_size)
            return TelegramPostMetadataBackfillResult(
                dry_run=dry_run,
                channels_inspected=1,
                batches_processed=1,
                candidates_inspected=1,
                captured=0,
                missing=0,
                transient_failures=0,
                permanent_failures=1,
                stale_candidates=0,
                unassigned_channels=0,
                sessions_parked=0,
                sessions_quarantined=0,
                sessions_skipped=0,
                sessions_requiring_attention=0,
            )

    monkeypatch.setattr(cli, "get_async_session_factory", object)
    monkeypatch.setattr(cli, "get_settings", object)
    monkeypatch.setattr(cli, "TelegramPostMetadataBackfiller", _Backfiller)

    exit_code = await cli.run(["--dry-run"])

    assert exit_code == 2
    assert "permanent_failures=1" in capsys.readouterr().out
