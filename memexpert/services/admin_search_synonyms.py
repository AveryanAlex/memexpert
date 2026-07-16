# ruff: noqa: TC001,TC003
"""Audited admin service for PostgreSQL-backed Meilisearch synonyms."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Final

from sqlalchemy import select

from memexpert.models.base import utcnow
from memexpert.models.enums import (
    SearchSynonymLocale,
    SearchSynonymRevisionStatus,
    SearchSynonymSyncStatus,
)
from memexpert.models.operations import OperationalAuditLog
from memexpert.models.search_synonyms import (
    SearchSynonymCatalog,
    SearchSynonymRevision,
    SearchSynonymSyncState,
)
from memexpert.schemas.search_synonyms import (
    SearchSynonymCatalogRead,
    SearchSynonymRevisionRead,
    SearchSynonymSyncStateRead,
    SearchSynonymValidationIssueRead,
    SearchSynonymValidationRead,
)
from memexpert.services.search_synonym_compiler import (
    SearchSynonymCompileResult,
    compile_search_synonyms,
    hash_synonym_map,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SEARCH_SYNONYM_SYNC_STATE_ID: Final = "meilisearch"
BUNDLED_SEED_DIRECTORY: Final = Path(__file__).resolve().parents[2] / "docs" / "research"
DESTRUCTIVE_KEY_REDUCTION_THRESHOLD: Final = 0.25


class AdminSearchSynonymError(RuntimeError):
    """Base error for safe admin synonym operations."""


class AdminSearchSynonymNotFoundError(AdminSearchSynonymError):
    """Raised when a catalog or requested immutable revision is absent."""


class AdminSearchSynonymConflictError(AdminSearchSynonymError):
    """Raised for stale versions or invalid lifecycle transitions."""


class AdminSearchSynonymPublishValidationError(AdminSearchSynonymError):
    """Raised when a draft contains publish-blocking compiler errors."""

    def __init__(self, validation: SearchSynonymValidationRead) -> None:
        super().__init__("The synonym draft contains publish-blocking validation errors.")
        self.validation = validation


class AdminSearchSynonymDestructiveChangeError(AdminSearchSynonymError):
    """Raised when a large key reduction has not been explicitly confirmed."""

    def __init__(self, *, previous_key_count: int, new_key_count: int) -> None:
        reduction = (previous_key_count - new_key_count) / previous_key_count
        super().__init__(
            "Publishing would remove more than 25% of the current synonym keys; "
            "confirm the destructive change explicitly.",
        )
        self.previous_key_count = previous_key_count
        self.new_key_count = new_key_count
        self.reduction_fraction = reduction


class AdminSearchSynonymSeedUnavailableError(AdminSearchSynonymError):
    """Raised when a bundled research seed is missing from the runtime image."""


class AdminSearchSynonymService:
    """Manage locale drafts and schedule combined settings reconciliation."""

    def __init__(self, session: AsyncSession, *, index_name: str) -> None:
        self._session = session
        self._index_name = index_name

    async def get_catalog(self, locale: SearchSynonymLocale) -> SearchSynonymCatalogRead:
        catalog = await self._get_catalog(locale)
        return await self._project_catalog(catalog)

    async def save_draft(
        self,
        *,
        admin_user_id: uuid.UUID,
        locale: SearchSynonymLocale,
        request_id: uuid.UUID,
        version: str,
        source_text: str,
        reason: str,
    ) -> SearchSynonymCatalogRead:
        return await self._replace_draft(
            admin_user_id=admin_user_id,
            locale=locale,
            request_id=request_id,
            version=version,
            source_text=source_text,
            reason=reason,
            action="save_search_synonym_draft",
        )

    async def import_seed(
        self,
        *,
        admin_user_id: uuid.UUID,
        locale: SearchSynonymLocale,
        request_id: uuid.UUID,
        version: str,
        reason: str,
    ) -> SearchSynonymCatalogRead:
        source_text = load_bundled_synonym_seed(locale)
        return await self._replace_draft(
            admin_user_id=admin_user_id,
            locale=locale,
            request_id=request_id,
            version=version,
            source_text=source_text,
            reason=reason,
            action="import_search_synonym_seed",
        )

    async def reset_draft(
        self,
        *,
        admin_user_id: uuid.UUID,
        locale: SearchSynonymLocale,
        request_id: uuid.UUID,
        version: str,
        reason: str,
        revision_id: uuid.UUID | None,
    ) -> SearchSynonymCatalogRead:
        catalog = await self._get_catalog(locale, lock=True)
        revisions = await self._get_revisions(catalog.id, lock=True)
        draft = _require_draft(revisions, locale=locale)
        self._assert_version(draft.version, version, target="Synonym draft")

        if revision_id is None:
            published = _find_revision(revisions, SearchSynonymRevisionStatus.PUBLISHED)
            source_text = "" if published is None else published.source_text
            restored_revision_id = None if published is None else published.id
        else:
            source_revision = next(
                (
                    revision
                    for revision in revisions
                    if revision.id == revision_id
                    and revision.status
                    in {
                        SearchSynonymRevisionStatus.PUBLISHED,
                        SearchSynonymRevisionStatus.ARCHIVED,
                    }
                ),
                None,
            )
            if source_revision is None:
                raise AdminSearchSynonymNotFoundError(
                    f"Immutable {locale.value.upper()} synonym revision {revision_id} does not exist.",
                )
            source_text = source_revision.source_text
            restored_revision_id = source_revision.id

        previous_hash = draft.compiled_hash
        result = compile_search_synonyms(source_text, locale=locale)
        _apply_compile_result(
            draft,
            source_text=source_text,
            result=result,
            admin_user_id=admin_user_id,
            change_note=reason,
        )
        self._session.add(
            OperationalAuditLog(
                admin_user_id=admin_user_id,
                request_id=request_id,
                action="reset_search_synonym_draft",
                target_kind="search_synonym_revision",
                target_id=str(draft.id),
                previous_values={"compiled_hash": previous_hash, "version": draft.version - 1},
                new_values={
                    "compiled_hash": draft.compiled_hash,
                    "compiler_version": draft.compiler_version,
                    "version": draft.version,
                    "restored_revision_id": (
                        None if restored_revision_id is None else str(restored_revision_id)
                    ),
                },
                note=reason,
            )
        )
        await self._session.commit()
        return await self._project_catalog(catalog)

    async def publish_draft(
        self,
        *,
        admin_user_id: uuid.UUID,
        locale: SearchSynonymLocale,
        request_id: uuid.UUID,
        version: str,
        reason: str,
        confirm_destructive: bool,
    ) -> SearchSynonymCatalogRead:
        catalog = await self._get_catalog(locale, lock=True)
        revisions = await self._get_revisions(catalog.id, lock=True)
        sync_state = await self._get_sync_state(lock=True)
        draft = _require_draft(revisions, locale=locale)
        self._assert_version(draft.version, version, target="Synonym draft")
        result = compile_search_synonyms(draft.source_text, locale=locale)
        if not result.valid:
            raise AdminSearchSynonymPublishValidationError(_validation_read(result))
        colliding_keys = await self._find_cross_locale_key_collisions(
            catalog_id=catalog.id,
            compiled_synonyms=result.compiled_synonyms,
        )
        if colliding_keys:
            validation = _validation_read(result)
            validation.valid = False
            validation.issues.extend(
                SearchSynonymValidationIssueRead(
                    level="error",
                    code="cross_locale_key_collision",
                    message="The normalized key is already published in another locale catalog.",
                    term=key,
                )
                for key in colliding_keys
            )
            raise AdminSearchSynonymPublishValidationError(validation)

        previous_published = _find_revision(revisions, SearchSynonymRevisionStatus.PUBLISHED)
        previous_key_count = (
            0 if previous_published is None else len(previous_published.compiled_synonyms)
        )
        new_key_count = len(result.compiled_synonyms)
        if (
            previous_key_count > 0
            and new_key_count < previous_key_count
            and (previous_key_count - new_key_count) / previous_key_count
            > DESTRUCTIVE_KEY_REDUCTION_THRESHOLD
            and not confirm_destructive
        ):
            raise AdminSearchSynonymDestructiveChangeError(
                previous_key_count=previous_key_count,
                new_key_count=new_key_count,
            )

        now = utcnow()
        if previous_published is not None:
            previous_published.status = SearchSynonymRevisionStatus.ARCHIVED
            previous_published.archived_at = now
            previous_published.archived_by_admin_user_id = admin_user_id

        draft.status = SearchSynonymRevisionStatus.PUBLISHED
        draft.compiled_synonyms = result.compiled_synonyms
        draft.compiler_version = result.compiler_version
        draft.compiled_hash = result.compiled_hash
        draft.validation = result.validation
        draft.stats = _stats_json(result)
        draft.change_note = reason
        draft.updated_by_admin_user_id = admin_user_id
        draft.published_by_admin_user_id = admin_user_id
        draft.published_at = now
        draft.archived_at = None
        draft.archived_by_admin_user_id = None
        await self._session.flush()

        next_revision_number = max(revision.revision_number for revision in revisions) + 1
        next_draft = SearchSynonymRevision(
            catalog_id=catalog.id,
            revision_number=next_revision_number,
            status=SearchSynonymRevisionStatus.DRAFT,
            source_text=draft.source_text,
            compiled_synonyms=result.compiled_synonyms,
            compiler_version=result.compiler_version,
            compiled_hash=result.compiled_hash,
            validation=result.validation,
            stats=_stats_json(result),
            change_note=f"Draft cloned from published revision {draft.revision_number}.",
            version=1,
            created_by_admin_user_id=admin_user_id,
            updated_by_admin_user_id=admin_user_id,
        )
        self._session.add(next_draft)
        await self._session.flush()

        desired_map, desired_revision_ids = await self._load_published_snapshot()
        desired_hash, _payload_bytes = hash_synonym_map(desired_map)
        sync_state.status = SearchSynonymSyncStatus.PENDING
        sync_state.desired_hash = desired_hash
        sync_state.desired_revision_ids = desired_revision_ids
        sync_state.requested_at = now
        sync_state.last_error = None
        sync_state.version += 1

        self._session.add(
            OperationalAuditLog(
                admin_user_id=admin_user_id,
                request_id=request_id,
                action="publish_search_synonym_revision",
                target_kind="search_synonym_revision",
                target_id=str(draft.id),
                previous_values={
                    "published_revision_id": (
                        None if previous_published is None else str(previous_published.id)
                    ),
                    "key_count": previous_key_count,
                },
                new_values={
                    "published_revision_id": str(draft.id),
                    "revision_number": draft.revision_number,
                    "key_count": new_key_count,
                    "compiled_hash": draft.compiled_hash,
                    "compiler_version": draft.compiler_version,
                    "sync_desired_hash": desired_hash,
                },
                note=reason,
            )
        )
        await self._session.commit()
        return await self._project_catalog(catalog)

    async def get_sync_state(self) -> SearchSynonymSyncStateRead:
        state = await self._get_sync_state()
        return await self._project_sync_state(state)

    async def retry_sync(
        self,
        *,
        admin_user_id: uuid.UUID,
        request_id: uuid.UUID,
        version: str,
        reason: str,
    ) -> SearchSynonymSyncStateRead:
        state = await self._get_sync_state(lock=True)
        if state.desired_hash is None or not state.desired_revision_ids:
            raise AdminSearchSynonymConflictError(
                "Publish at least one synonym revision before requesting synchronization.",
            )

        # Retry is idempotent and does not replace desired state. Monitoring
        # updates advance the row version every reconciliation pass, so a stale
        # display version must not turn a safe retry into a spurious conflict.
        previous_status = state.status
        now = utcnow()
        state.status = SearchSynonymSyncStatus.PENDING
        state.requested_at = now
        state.last_error = None
        state.version += 1
        self._session.add(
            OperationalAuditLog(
                admin_user_id=admin_user_id,
                request_id=request_id,
                action="retry_search_synonym_sync",
                target_kind="search_synonym_sync_state",
                target_id=state.id,
                previous_values={
                    "status": previous_status.value,
                    "reviewed_version": version,
                    "current_version": state.version - 1,
                },
                new_values={"status": state.status.value, "desired_hash": state.desired_hash},
                note=reason,
            )
        )
        await self._session.commit()
        return await self._project_sync_state(state)

    async def _replace_draft(
        self,
        *,
        admin_user_id: uuid.UUID,
        locale: SearchSynonymLocale,
        request_id: uuid.UUID,
        version: str,
        source_text: str,
        reason: str,
        action: str,
    ) -> SearchSynonymCatalogRead:
        catalog = await self._get_catalog(locale, lock=True)
        revisions = await self._get_revisions(catalog.id, lock=True)
        draft = _require_draft(revisions, locale=locale)
        self._assert_version(draft.version, version, target="Synonym draft")
        previous_hash = draft.compiled_hash
        result = compile_search_synonyms(source_text, locale=locale)
        _apply_compile_result(
            draft,
            source_text=source_text,
            result=result,
            admin_user_id=admin_user_id,
            change_note=reason,
        )
        self._session.add(
            OperationalAuditLog(
                admin_user_id=admin_user_id,
                request_id=request_id,
                action=action,
                target_kind="search_synonym_revision",
                target_id=str(draft.id),
                previous_values={"compiled_hash": previous_hash, "version": draft.version - 1},
                new_values={
                    "compiled_hash": draft.compiled_hash,
                    "compiler_version": draft.compiler_version,
                    "version": draft.version,
                    "valid": result.valid,
                    "compiled_key_count": result.stats["compiled_key_count"],
                },
                note=reason,
            )
        )
        await self._session.commit()
        return await self._project_catalog(catalog)

    async def _get_catalog(
        self,
        locale: SearchSynonymLocale,
        *,
        lock: bool = False,
    ) -> SearchSynonymCatalog:
        statement = select(SearchSynonymCatalog).where(SearchSynonymCatalog.locale == locale)
        if lock:
            statement = statement.with_for_update()
        catalog = (await self._session.execute(statement)).scalar_one_or_none()
        if catalog is None:
            raise AdminSearchSynonymNotFoundError(
                f"The {locale.value.upper()} synonym catalog is not initialized.",
            )
        return catalog

    async def _get_revisions(
        self,
        catalog_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> list[SearchSynonymRevision]:
        statement = (
            select(SearchSynonymRevision)
            .where(SearchSynonymRevision.catalog_id == catalog_id)
            .order_by(SearchSynonymRevision.revision_number.desc())
        )
        if lock:
            statement = statement.with_for_update()
        return list((await self._session.execute(statement)).scalars().all())

    async def _get_sync_state(self, *, lock: bool = False) -> SearchSynonymSyncState:
        statement = select(SearchSynonymSyncState).where(
            SearchSynonymSyncState.id == SEARCH_SYNONYM_SYNC_STATE_ID
        )
        if lock:
            statement = statement.with_for_update()
        state = (await self._session.execute(statement)).scalar_one_or_none()
        if state is None:
            raise AdminSearchSynonymNotFoundError("The synonym sync state is not initialized.")
        return state

    async def _load_published_snapshot(self) -> tuple[dict[str, list[str]], dict[str, str]]:
        rows = (
            await self._session.execute(
                select(SearchSynonymRevision, SearchSynonymCatalog.locale)
                .join(SearchSynonymCatalog, SearchSynonymCatalog.id == SearchSynonymRevision.catalog_id)
                .where(SearchSynonymRevision.status == SearchSynonymRevisionStatus.PUBLISHED)
                .order_by(SearchSynonymCatalog.locale, SearchSynonymRevision.revision_number)
            )
        ).all()
        combined: dict[str, list[str]] = {}
        key_locales: dict[str, SearchSynonymLocale] = {}
        revision_ids: dict[str, str] = {}
        for revision, locale in rows:
            duplicate_keys = sorted(set(combined).intersection(revision.compiled_synonyms))
            if duplicate_keys:
                first_key = duplicate_keys[0]
                raise AdminSearchSynonymConflictError(
                    "Published synonym catalogs contain a duplicate normalized key "
                    f"{first_key!r} in {key_locales[first_key].value.upper()} and "
                    f"{locale.value.upper()}.",
                )
            combined.update(revision.compiled_synonyms)
            key_locales.update(dict.fromkeys(revision.compiled_synonyms, locale))
            revision_ids[locale.value] = str(revision.id)
        return {key: combined[key] for key in sorted(combined)}, revision_ids

    async def _find_cross_locale_key_collisions(
        self,
        *,
        catalog_id: uuid.UUID,
        compiled_synonyms: dict[str, list[str]],
    ) -> list[str]:
        published_maps = (
            await self._session.execute(
                select(SearchSynonymRevision.compiled_synonyms).where(
                    SearchSynonymRevision.catalog_id != catalog_id,
                    SearchSynonymRevision.status == SearchSynonymRevisionStatus.PUBLISHED,
                )
            )
        ).scalars()
        other_keys: set[str] = set()
        for published_map in published_maps:
            other_keys.update(published_map)
        return sorted(other_keys.intersection(compiled_synonyms))

    async def _project_catalog(self, catalog: SearchSynonymCatalog) -> SearchSynonymCatalogRead:
        revisions = await self._get_revisions(catalog.id)
        draft = _require_draft(revisions, locale=catalog.locale)
        published = _find_revision(revisions, SearchSynonymRevisionStatus.PUBLISHED)
        history = [
            _project_revision(revision)
            for revision in revisions
            if revision.status is SearchSynonymRevisionStatus.ARCHIVED
        ]
        return SearchSynonymCatalogRead(
            locale=catalog.locale,
            draft=_project_revision(draft),
            published=None if published is None else _project_revision(published),
            history=history,
        )

    async def _project_sync_state(self, state: SearchSynonymSyncState) -> SearchSynonymSyncStateRead:
        desired_revisions: dict[str, int] = {}
        parsed_ids: dict[str, uuid.UUID] = {}
        for locale, raw_revision_id in state.desired_revision_ids.items():
            try:
                parsed_ids[locale] = uuid.UUID(raw_revision_id)
            except (TypeError, ValueError):
                continue
        if parsed_ids:
            revision_rows = (
                await self._session.execute(
                    select(SearchSynonymRevision.id, SearchSynonymRevision.revision_number).where(
                        SearchSynonymRevision.id.in_(tuple(parsed_ids.values()))
                    )
                )
            ).all()
            revision_numbers = {
                revision_id: revision_number
                for revision_id, revision_number in revision_rows
            }
            desired_revisions = {
                locale: revision_numbers[revision_id]
                for locale, revision_id in parsed_ids.items()
                if revision_id in revision_numbers
            }

        return SearchSynonymSyncStateRead(
            index_name=self._index_name,
            status=state.status,
            desired_hash=state.desired_hash,
            applied_hash=state.applied_hash,
            actual_hash=state.actual_hash,
            desired_revisions=desired_revisions,
            last_task_uid=state.provider_task_uid,
            requested_at=state.requested_at,
            last_checked_at=state.last_attempt_at,
            last_applied_at=state.last_success_at,
            safe_error=state.last_error,
            updated_at=state.updated_at,
            version=str(state.version),
        )

    @staticmethod
    def _assert_version(actual_version: int, requested_version: str, *, target: str) -> None:
        if str(actual_version) != requested_version:
            raise AdminSearchSynonymConflictError(
                f"{target} changed; reload it before applying this mutation.",
            )


def load_bundled_synonym_seed(locale: SearchSynonymLocale) -> str:
    """Read one repository-bundled research seed using a stable root path."""

    seed_path = BUNDLED_SEED_DIRECTORY / f"meme-search-synonyms-{locale.value}.txt"
    try:
        return seed_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AdminSearchSynonymSeedUnavailableError(
            f"The bundled {locale.value.upper()} synonym seed is unavailable.",
        ) from exc


def _apply_compile_result(
    draft: SearchSynonymRevision,
    *,
    source_text: str,
    result: SearchSynonymCompileResult,
    admin_user_id: uuid.UUID,
    change_note: str,
) -> None:
    draft.source_text = source_text
    draft.compiled_synonyms = result.compiled_synonyms
    draft.compiler_version = result.compiler_version
    draft.compiled_hash = result.compiled_hash
    draft.validation = result.validation
    draft.stats = _stats_json(result)
    draft.change_note = change_note
    draft.updated_by_admin_user_id = admin_user_id
    draft.version += 1


def _require_draft(
    revisions: list[SearchSynonymRevision],
    *,
    locale: SearchSynonymLocale,
) -> SearchSynonymRevision:
    draft = _find_revision(revisions, SearchSynonymRevisionStatus.DRAFT)
    if draft is None:
        raise AdminSearchSynonymNotFoundError(
            f"The {locale.value.upper()} synonym catalog has no mutable draft.",
        )
    return draft


def _find_revision(
    revisions: list[SearchSynonymRevision],
    status: SearchSynonymRevisionStatus,
) -> SearchSynonymRevision | None:
    return next((revision for revision in revisions if revision.status is status), None)


def _project_revision(revision: SearchSynonymRevision) -> SearchSynonymRevisionRead:
    raw_issues = revision.validation.get("issues", [])
    issue_rows = raw_issues if isinstance(raw_issues, list) else []
    validation = SearchSynonymValidationRead(
        valid=bool(revision.validation.get("valid", False)),
        group_count=_stat(revision, "group_count"),
        compiled_key_count=_stat(revision, "compiled_key_count"),
        edge_count=_stat(revision, "edge_count"),
        payload_bytes=_stat(revision, "payload_bytes"),
        issues=[
            SearchSynonymValidationIssueRead.model_validate(issue)
            for issue in issue_rows
            if isinstance(issue, dict)
        ],
    )
    return SearchSynonymRevisionRead(
        id=revision.id,
        revision_number=revision.revision_number,
        status=revision.status,
        source_text=revision.source_text,
        compiler_version=revision.compiler_version,
        compiled_hash=revision.compiled_hash,
        validation=validation,
        change_note=revision.change_note,
        published_at=revision.published_at,
        created_at=revision.created_at,
        updated_at=revision.updated_at,
        version=str(revision.version),
    )


def _validation_read(result: SearchSynonymCompileResult) -> SearchSynonymValidationRead:
    return SearchSynonymValidationRead(
        valid=result.valid,
        group_count=result.stats["group_count"],
        compiled_key_count=result.stats["compiled_key_count"],
        edge_count=result.stats["edge_count"],
        payload_bytes=result.stats["payload_bytes"],
        issues=[SearchSynonymValidationIssueRead.model_validate(issue) for issue in result.issues],
    )


def _stat(revision: SearchSynonymRevision, name: str) -> int:
    value = revision.stats.get(name, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _stats_json(result: SearchSynonymCompileResult) -> dict[str, object]:
    return {key: value for key, value in result.stats.items()}


__all__ = [
    "AdminSearchSynonymConflictError",
    "AdminSearchSynonymDestructiveChangeError",
    "AdminSearchSynonymError",
    "AdminSearchSynonymNotFoundError",
    "AdminSearchSynonymPublishValidationError",
    "AdminSearchSynonymSeedUnavailableError",
    "AdminSearchSynonymService",
    "BUNDLED_SEED_DIRECTORY",
    "DESTRUCTIVE_KEY_REDUCTION_THRESHOLD",
    "SEARCH_SYNONYM_SYNC_STATE_ID",
    "load_bundled_synonym_seed",
]
