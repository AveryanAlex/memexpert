"""Focused tests for shared cleanup helpers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memexpert.api.routes.v1 import collections as collection_routes
from memexpert.api.routes.v1 import memes as meme_routes
from memexpert.models.enums import ModerationAction, ModerationReason, SourcePlatform
from memexpert.schemas.admin import (
    AdminMemeDeleteRequest,
    AdminModerationReportResolveRequest,
    AdminSourceChannelCreateRequest,
)
from memexpert.schemas.report import MemeReportCreateRequest
from memexpert.services import (
    CollectionNotFoundError,
    CollectionVerificationRequiredError,
    CollectionWriteAccessError,
    DuplicateCollectionInviteError,
    GuestCollectionAccessError,
    InvalidCollectionInviteError,
    InvalidCollectionMembershipError,
    InvalidCollectionTitleError,
    InvalidPinnedMemeOrderError,
    PinLimitExceededError,
)


def test_collection_route_error_mapping_preserves_status_codes() -> None:
    assert collection_routes._collection_http_error(CollectionNotFoundError("missing")).status_code == 404
    assert collection_routes._collection_http_error(CollectionWriteAccessError("readonly")).status_code == 403
    assert collection_routes._collection_http_error(GuestCollectionAccessError("guest")).status_code == 403
    assert collection_routes._collection_http_error(CollectionVerificationRequiredError("verify")).status_code == 403
    assert collection_routes._collection_http_error(DuplicateCollectionInviteError("duplicate")).status_code == 409
    assert collection_routes._collection_http_error(InvalidCollectionMembershipError("role")).status_code == 409
    assert collection_routes._collection_http_error(InvalidCollectionInviteError("invite")).status_code == 400
    assert collection_routes._collection_http_error(InvalidCollectionTitleError("title")).status_code == 400


def test_meme_route_error_mapping_preserves_status_codes() -> None:
    assert meme_routes._collection_http_error(CollectionNotFoundError("missing")).status_code == 404
    assert meme_routes._collection_http_error(CollectionWriteAccessError("readonly")).status_code == 403
    assert meme_routes._collection_http_error(GuestCollectionAccessError("guest")).status_code == 403
    assert meme_routes._collection_http_error(InvalidPinnedMemeOrderError("order")).status_code == 409
    assert meme_routes._collection_http_error(PinLimitExceededError("limit")).status_code == 409
    assert meme_routes._collection_http_error(CollectionVerificationRequiredError("verify")).status_code == 400
    assert meme_routes._collection_http_error(DuplicateCollectionInviteError("duplicate")).status_code == 400


def test_shared_optional_note_normalization_keeps_schema_behavior() -> None:
    report = MemeReportCreateRequest(reason=ModerationReason.SPAM, note="  spam note  ")
    admin_resolve = AdminModerationReportResolveRequest(
        action=ModerationAction.NO_ACTION,
        reason=ModerationReason.OTHER,
        note="  moderator note  ",
    )
    blank_note = MemeReportCreateRequest(reason=ModerationReason.OTHER, note="   ")

    assert report.note == "spam note"
    assert admin_resolve.note == "moderator note"
    assert blank_note.note is None


def test_shared_required_text_normalization_keeps_schema_behavior() -> None:
    source = AdminSourceChannelCreateRequest(
        platform=SourcePlatform.TELEGRAM,
        platform_id="  channel-id  ",
        title="  Source Title  ",
        username="  source_username  ",
        session_id="   ",
    )
    delete_request = AdminMemeDeleteRequest(confirmation="  meme-id  ", note="  remove duplicate  ")

    assert source.platform_id == "channel-id"
    assert source.title == "Source Title"
    assert source.username == "source_username"
    assert source.session_id is None
    assert delete_request.confirmation == "meme-id"
    assert delete_request.note == "remove duplicate"

    with pytest.raises(ValidationError):
        AdminSourceChannelCreateRequest(
            platform=SourcePlatform.TELEGRAM,
            platform_id="   ",
            title="Source Title",
        )
