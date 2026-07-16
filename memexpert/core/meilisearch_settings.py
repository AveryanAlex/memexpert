"""Meilisearch index-settings adapter used by the singleton scheduler."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

import httpx

from memexpert.core._network import is_timeout_exception
from memexpert.core.config import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import Mapping


class MeilisearchSettingsError(RuntimeError):
    """Base error for safe Meilisearch settings reconciliation failures."""


class MeilisearchSettingsProviderUnavailableError(MeilisearchSettingsError):
    """Raised when the settings provider cannot be reached."""


class MeilisearchSettingsTimeoutError(MeilisearchSettingsError):
    """Raised when a settings request or asynchronous task exceeds its deadline."""


class MeilisearchSettingsMalformedResponseError(MeilisearchSettingsError):
    """Raised when a settings response cannot be trusted."""


class MeilisearchSettingsRejectedError(MeilisearchSettingsError):
    """Raised when Meilisearch rejects a settings update or its task fails."""


class MeilisearchSettingsClientProtocol(Protocol):
    """Narrow settings surface consumed by the reconciliation service."""

    async def get_synonyms(self) -> dict[str, list[str]]: ...

    async def submit_synonyms(self, synonyms: Mapping[str, list[str]]) -> int: ...

    async def wait_for_task(self, task_uid: int) -> None: ...


class MeilisearchSettingsClient:
    """Lazy adapter for reading and replacing the complete synonym map.

    Meilisearch synonym updates are asynchronous full replacements. Submission
    and task waiting intentionally remain separate so the durable sync row can
    record the provider task identifier before the potentially long wait.
    """

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: Any | None = None
        self._index: Any | None = None
        self._index_lock = asyncio.Lock()

    async def get_synonyms(self) -> dict[str, list[str]]:
        """Fetch the complete synonym map from the configured index."""

        index = await self._ensure_index_client()
        try:
            async with asyncio.timeout(self._settings.pipeline_meilisearch_timeout_seconds):
                raw_synonyms = await index.get_synonyms()
        except Exception as exc:
            _raise_settings_error_from(exc, operation="get_synonyms")
        return _coerce_synonym_map(raw_synonyms)

    async def submit_synonyms(self, synonyms: Mapping[str, list[str]]) -> int:
        """Submit one complete replacement and return its asynchronous task UID."""

        payload = {key: list(values) for key, values in synonyms.items()}
        if not payload:
            raise MeilisearchSettingsRejectedError(
                "Refusing to submit an empty Meilisearch synonym map.",
            )
        index = await self._ensure_index_client()
        try:
            async with asyncio.timeout(self._settings.pipeline_meilisearch_timeout_seconds):
                task = await index.update_synonyms(payload)
        except Exception as exc:
            _raise_settings_error_from(exc, operation="update_synonyms")
        return _extract_task_uid(task, operation="update_synonyms")

    async def wait_for_task(self, task_uid: int) -> None:
        """Wait for a settings task using the settings-specific long deadline."""

        client = await self._ensure_client()
        timeout_seconds = self._settings.meilisearch_settings_task_timeout_seconds
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await client.wait_for_task(
                    task_uid,
                    timeout_in_ms=max(1, round(timeout_seconds * 1000)),
                    raise_for_status=False,
                )
        except Exception as exc:
            _raise_settings_error_from(exc, operation="wait_for_settings_task")

        status = getattr(result, "status", None)
        if isinstance(result, dict):
            status = result.get("status", status)
        if status == "failed":
            raise MeilisearchSettingsRejectedError(
                f"Meilisearch settings task {task_uid} failed.",
            )
        if status != "succeeded":
            raise MeilisearchSettingsMalformedResponseError(
                f"Meilisearch settings task {task_uid} returned an unexpected status.",
            )

    async def aclose(self) -> None:
        """Close the lazily-created SDK client and release its HTTP pool."""

        client = self._client
        self._client = None
        self._index = None
        if client is not None:
            await client.aclose()

    async def _ensure_index_client(self) -> Any:
        if self._index is not None:
            return self._index
        async with self._index_lock:
            if self._index is not None:
                return self._index
            client = await self._ensure_client()
            try:
                async with asyncio.timeout(self._settings.pipeline_meilisearch_timeout_seconds):
                    self._index = await client.get_or_create_index(
                        self._settings.pipeline_meilisearch_index_name,
                        primary_key="id",
                    )
            except Exception as exc:
                _raise_settings_error_from(exc, operation="ensure_index")
        return self._index

    async def _ensure_client(self) -> Any:
        if self._client is None:
            from meilisearch_python_sdk import AsyncClient

            self._client = AsyncClient(
                url=self._settings.meilisearch_url,
                api_key=self._settings.meilisearch_master_key,
                timeout=max(1, int(self._settings.pipeline_meilisearch_timeout_seconds)),
            )
        return self._client


def _coerce_synonym_map(raw_synonyms: object) -> dict[str, list[str]]:
    if raw_synonyms is None:
        return {}
    if not isinstance(raw_synonyms, dict):
        raise MeilisearchSettingsMalformedResponseError(
            "Meilisearch returned a malformed synonym settings payload.",
        )

    synonyms: dict[str, list[str]] = {}
    for raw_key, raw_values in raw_synonyms.items():
        if not isinstance(raw_key, str) or not isinstance(raw_values, list):
            raise MeilisearchSettingsMalformedResponseError(
                "Meilisearch returned a malformed synonym settings payload.",
            )
        if any(not isinstance(value, str) for value in raw_values):
            raise MeilisearchSettingsMalformedResponseError(
                "Meilisearch returned a malformed synonym settings payload.",
            )
        synonyms[raw_key] = [value for value in raw_values if isinstance(value, str)]
    return synonyms


def _extract_task_uid(task: object, *, operation: str) -> int:
    task_uid = getattr(task, "task_uid", None)
    if isinstance(task, dict):
        task_uid = task.get("taskUid", task.get("task_uid", task_uid))
    if type(task_uid) is not int or task_uid < 0:
        raise MeilisearchSettingsMalformedResponseError(
            f"Meilisearch {operation} did not return a valid task identifier.",
        )
    return task_uid


def _raise_settings_error_from(exc: BaseException, *, operation: str) -> NoReturn:
    if isinstance(exc, MeilisearchSettingsError):
        raise exc
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
        raise MeilisearchSettingsTimeoutError(f"Meilisearch {operation} timed out.") from exc
    if is_timeout_exception(exc) or exc.__class__.__name__ == "MeilisearchTimeoutError":
        raise MeilisearchSettingsTimeoutError(f"Meilisearch {operation} timed out.") from exc
    if exc.__class__.__name__ == "MeilisearchTaskFailedError":
        raise MeilisearchSettingsRejectedError(f"Meilisearch {operation} task failed.") from exc

    status_code = _extract_status_code(exc)
    if status_code is not None and 400 <= status_code < 500:
        raise MeilisearchSettingsRejectedError(
            f"Meilisearch {operation} was rejected with status {status_code}.",
        ) from exc
    if status_code is not None:
        raise MeilisearchSettingsProviderUnavailableError(
            f"Meilisearch {operation} failed with status {status_code}.",
        ) from exc
    if exc.__class__.__module__.startswith("pydantic"):
        raise MeilisearchSettingsMalformedResponseError(
            f"Meilisearch {operation} returned a malformed response.",
        ) from exc
    raise MeilisearchSettingsProviderUnavailableError(
        f"Meilisearch {operation} failed ({type(exc).__name__}).",
    ) from exc


def _extract_status_code(exc: BaseException) -> int | None:
    status_code = getattr(exc, "status_code", None)
    return status_code if isinstance(status_code, int) else None


__all__ = [
    "MeilisearchSettingsClient",
    "MeilisearchSettingsClientProtocol",
    "MeilisearchSettingsError",
    "MeilisearchSettingsMalformedResponseError",
    "MeilisearchSettingsProviderUnavailableError",
    "MeilisearchSettingsRejectedError",
    "MeilisearchSettingsTimeoutError",
]
