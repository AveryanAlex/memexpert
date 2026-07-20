"""Unit contracts for Replay & Repair API schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memexpert.models.enums import ContentPipelineStage, RecoveryCapability, RecoveryReplayScope
from memexpert.schemas.admin_recovery import (
    RecoveryActionRead,
    RecoveryActionScopeRequirementsRead,
    RecoveryQueryFilters,
)


def test_recovery_action_serializes_effective_requirements_by_scope() -> None:
    action = RecoveryActionRead(
        capability=RecoveryCapability.REPLAY_STAGE,
        available=True,
        scopes=[RecoveryReplayScope.STAGE_ONLY, RecoveryReplayScope.STAGE_AND_DEPENDENTS],
        scope_requirements={
            RecoveryReplayScope.STAGE_ONLY: RecoveryActionScopeRequirementsRead(),
            RecoveryReplayScope.STAGE_AND_DEPENDENTS: RecoveryActionScopeRequirementsRead(
                risks=["Provider output may differ."],
                required_acknowledgements=["terminal_override"],
            ),
        },
    )

    serialized = action.model_dump(mode="json")
    assert serialized["scope_requirements"] == {
        "stage_only": {
            "warnings": [],
            "risks": [],
            "required_acknowledgements": [],
        },
        "stage_and_dependents": {
            "warnings": [],
            "risks": ["Provider output may differ."],
            "required_acknowledgements": ["terminal_override"],
        },
    }


def test_recovery_action_scope_rejects_unknown_acknowledgements() -> None:
    with pytest.raises(ValidationError, match="terminal_override"):
        RecoveryActionRead.model_validate(
            {
                "capability": "replay_stage",
                "available": True,
                "scopes": ["stage_and_dependents"],
                "scope_requirements": {
                    "stage_and_dependents": {
                        "required_acknowledgements": ["typed_confirmation"],
                    }
                },
            }
        )


def test_successful_stage_query_requires_one_non_ingest_stage() -> None:
    filters = RecoveryQueryFilters(
        successful_stage=True,
        stage=ContentPipelineStage.OCR,
    )

    assert filters.model_dump(mode="json") == {
        "bucket": None,
        "kind": None,
        "source_channel_id": None,
        "stage": "ocr",
        "reason": None,
        "query": None,
        "outdated_web_video": False,
        "successful_stage": True,
    }

    for invalid in (
        {"successful_stage": True},
        {"successful_stage": True, "stage": "ingest"},
        {"successful_stage": True, "stage": "ocr", "kind": "pipeline_stage"},
        {"successful_stage": True, "stage": "ocr", "outdated_web_video": True},
    ):
        with pytest.raises(ValidationError):
            RecoveryQueryFilters.model_validate(invalid)
