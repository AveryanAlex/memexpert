"""Bounded, secret-safe resolution of public Telegram channel references."""

from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from pydantic import SecretStr

    from memexpert.core.config import Settings


ADMIN_TELEGRAM_RESOLVE_TIMEOUT_SECONDS = 10.0
_TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_RESERVED_TELEGRAM_PATHS = frozenset(
    {"addstickers", "c", "joinchat", "login", "proxy", "s", "share", "socks"},
)


class AdminTelegramChannelResolverError(Exception):
    """A safe, operator-facing public channel resolution failure."""


@dataclass(frozen=True, slots=True)
class NormalizedPublicTelegramReference:
    username: str
    canonical_url: str


@dataclass(frozen=True, slots=True)
class ResolvedAdminTelegramChannel:
    """Secret-free canonical Telegram channel metadata.

    ``platform_id`` intentionally uses the lowercase public username so a cold
    crawler client can resolve it without persisted access-hash material. Public
    username renames require later operator reconciliation; rename tracking is
    deliberately outside this bounded workflow.
    """

    platform_id: str
    username: str
    title: str
    subscriber_count: int | None


def normalize_public_telegram_reference(reference: str) -> NormalizedPublicTelegramReference:
    """Accept only a public handle or a single-path Telegram public URL."""

    value = reference.strip()
    if not value:
        raise AdminTelegramChannelResolverError("Enter a public Telegram channel link or @handle.")

    if value.startswith("@"):  # explicit public handle
        return _normalized_username(value[1:])
    if _TELEGRAM_USERNAME_RE.fullmatch(value) is not None:
        return _normalized_username(value)

    try:
        parsed = urlparse(value if "://" in value else f"https://{value}")
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path_parts = [part for part in parsed.path.split("/") if part]
        is_public_telegram_url = (
            parsed.scheme.lower() in {"http", "https"}
            and host in {"t.me", "telegram.me"}
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and not parsed.query
            and not parsed.fragment
            and len(path_parts) == 1
        )
    except ValueError:
        is_public_telegram_url = False
        path_parts = []
    if not is_public_telegram_url:
        raise AdminTelegramChannelResolverError(
            "Only public t.me or telegram.me channel links and public handles are supported.",
        )
    return _normalized_username(path_parts[0].removeprefix("@"))


async def resolve_admin_telegram_channel(
    *,
    settings: Settings,
    string_session: SecretStr,
    reference: str,
    timeout_seconds: float = ADMIN_TELEGRAM_RESOLVE_TIMEOUT_SECONDS,
) -> ResolvedAdminTelegramChannel:
    """Resolve one public channel through an authorized Telethon StringSession."""

    normalized = normalize_public_telegram_reference(reference)
    api_id = settings.telegram_api_id
    api_hash = settings.telegram_api_hash
    if api_id is None or api_hash is None:
        raise AdminTelegramChannelResolverError("Telegram API credentials are not configured.")

    try:
        client = _build_telegram_client(
            string_session=string_session,
            api_id=api_id,
            api_hash=api_hash,
        )
    except Exception:
        raise AdminTelegramChannelResolverError("The selected Telegram account could not be opened.") from None

    try:
        async with asyncio.timeout(timeout_seconds):
            await client.connect()
            if not await client.is_user_authorized():
                raise AdminTelegramChannelResolverError("The selected Telegram account is no longer authorized.")
            entity = await client.get_entity(normalized.username)
            if not isinstance(entity, _telegram_channel_type()):
                raise AdminTelegramChannelResolverError("That public Telegram reference is not a channel.")
            channel_id = getattr(entity, "id", None)
            username = getattr(entity, "username", None)
            title = getattr(entity, "title", None)
            if not isinstance(channel_id, int) or channel_id <= 0:
                raise AdminTelegramChannelResolverError("Telegram returned an invalid channel identity.")
            if not isinstance(username, str) or _TELEGRAM_USERNAME_RE.fullmatch(username) is None:
                raise AdminTelegramChannelResolverError("Only public Telegram channels with a handle are supported.")
            if not isinstance(title, str) or not title.strip():
                title = username
            subscriber_count = getattr(entity, "participants_count", None)
            normalized_subscriber_count = (
                subscriber_count if isinstance(subscriber_count, int) and subscriber_count >= 0 else None
            )
            return ResolvedAdminTelegramChannel(
                platform_id=username.lower(),
                username=username.lower(),
                title=title.strip(),
                subscriber_count=normalized_subscriber_count,
            )
    except TimeoutError:
        raise AdminTelegramChannelResolverError("Telegram did not respond in time. Try again.") from None
    except AdminTelegramChannelResolverError:
        raise
    except Exception:
        raise AdminTelegramChannelResolverError(
            "Telegram could not resolve that public channel. Check the reference and try again.",
        ) from None
    finally:
        try:
            disconnect_result = client.disconnect()
            if inspect.isawaitable(disconnect_result):
                async with asyncio.timeout(min(timeout_seconds, 2.0)):
                    await disconnect_result
        except Exception:
            pass


def _normalized_username(username: str) -> NormalizedPublicTelegramReference:
    if (
        _TELEGRAM_USERNAME_RE.fullmatch(username) is None
        or username.casefold() in _RESERVED_TELEGRAM_PATHS
    ):
        raise AdminTelegramChannelResolverError(
            "Enter a public Telegram channel handle using 5-32 letters, digits, or underscores.",
        )
    canonical_username = username.lower()
    return NormalizedPublicTelegramReference(
        username=canonical_username,
        canonical_url=f"https://t.me/{canonical_username}",
    )


def _build_telegram_client(*, string_session: SecretStr, api_id: int, api_hash: SecretStr):
    from telethon import TelegramClient  # noqa: PLC0415
    from telethon.sessions import StringSession  # noqa: PLC0415

    return TelegramClient(
        StringSession(string_session.get_secret_value()),
        api_id,
        api_hash.get_secret_value(),
    )


def _telegram_channel_type() -> type[object]:
    from telethon.tl.types import Channel  # noqa: PLC0415

    return Channel


__all__ = [
    "ADMIN_TELEGRAM_RESOLVE_TIMEOUT_SECONDS",
    "AdminTelegramChannelResolverError",
    "NormalizedPublicTelegramReference",
    "ResolvedAdminTelegramChannel",
    "normalize_public_telegram_reference",
    "resolve_admin_telegram_channel",
]
