"""Telegram private-chat meme library management router."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from memexpert.core.config import Settings, get_settings
from memexpert.core.database import get_async_session_factory
from memexpert.models.enums import AccountStatus, AccountType, CollectionKind
from memexpert.models.user import User
from memexpert.services import (
    CollectionService,
    CollectionServiceError,
    CollectionWriteAccessError,
    GuestCollectionAccessError,
    InvalidCollectionTitleError,
    InvalidPinnedMemeOrderError,
    MemeNotFoundError,
    PinLimitExceededError,
    UserNotFoundError,
)

if TYPE_CHECKING:
    from aiogram.types import MaybeInaccessibleMessage, Message
    from sqlalchemy.ext.asyncio import AsyncSession

    from memexpert.core.database import AsyncSessionFactory
    from memexpert.schemas import CollectionMemeRead, CollectionRead, MemeLibraryRead, PinnedMemeRead

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "pml"
PAGE_SIZE = 5


class PrivateLibraryCollectionService(Protocol):
    async def get_meme_library(self, *, user_id: object) -> MemeLibraryRead: ...

    async def list_favorite_memes(self, *, user_id: object) -> list[CollectionMemeRead]: ...

    async def unfavorite_meme(self, *, user_id: object, meme_id: object) -> bool: ...

    async def list_collection_memes(self, *, collection_id: object, user_id: object) -> list[CollectionMemeRead]: ...

    async def remove_meme_from_active_collection(self, *, user_id: object, meme_id: object) -> bool: ...

    async def list_pinned_memes(self, *, user_id: object) -> list[PinnedMemeRead]: ...

    async def pin_meme(self, *, user_id: object, meme_id: object) -> PinnedMemeRead: ...

    async def unpin_meme(self, *, user_id: object, meme_id: object) -> bool: ...

    async def reorder_pins(self, *, user_id: object, meme_ids: list[uuid.UUID]) -> list[PinnedMemeRead]: ...

    async def list_collections_for_user(self, *, user_id: object) -> list[CollectionRead]: ...

    async def create_custom_collection(self, *, owner_user_id: object, title: str) -> CollectionRead: ...

    async def delete_custom_collection(self, *, collection_id: object, user_id: object) -> bool: ...

    async def update_active_save_collection(self, *, user_id: object, collection_id: object) -> object: ...


type PrivateLibraryCollectionServiceFactory = Callable[[AsyncSession], PrivateLibraryCollectionService]


def build_private_library_router(
    *,
    settings: Settings | None = None,
    session_factory: AsyncSessionFactory | None = None,
    collection_service_factory: PrivateLibraryCollectionServiceFactory | None = None,
) -> Router:
    """Build the private-message library management router."""

    _ = settings or get_settings()
    resolved_session_factory = session_factory or get_async_session_factory()
    resolved_service_factory = collection_service_factory or (lambda session: CollectionService(session))

    router = Router(name="private-library")

    @router.message(F.chat.type == "private", Command("library"))
    async def handle_library_menu(message: Message) -> None:
        await show_library_menu(
            message=message,
            session_factory=resolved_session_factory,
            collection_service_factory=resolved_service_factory,
        )

    @router.message(F.chat.type == "private", Command("favorites"))
    async def handle_favorites(message: Message) -> None:
        await show_favorites(
            message=message,
            page=0,
            session_factory=resolved_session_factory,
            collection_service_factory=resolved_service_factory,
        )

    @router.message(F.chat.type == "private", Command("pins"))
    async def handle_pins(message: Message) -> None:
        await show_pins(
            message=message,
            page=0,
            session_factory=resolved_session_factory,
            collection_service_factory=resolved_service_factory,
        )

    @router.message(F.chat.type == "private", Command("active"))
    async def handle_active_collection(message: Message) -> None:
        await show_active_collection(
            message=message,
            page=0,
            session_factory=resolved_session_factory,
            collection_service_factory=resolved_service_factory,
        )

    @router.message(F.chat.type == "private", Command("collections"))
    async def handle_collections(message: Message) -> None:
        await show_collections(
            message=message,
            page=0,
            session_factory=resolved_session_factory,
            collection_service_factory=resolved_service_factory,
        )

    @router.message(F.chat.type == "private", Command("collection_create"))
    async def handle_collection_create(message: Message, command: CommandObject) -> None:
        await create_collection(
            message=message,
            title=(command.args or "").strip(),
            session_factory=resolved_session_factory,
            collection_service_factory=resolved_service_factory,
        )

    @router.callback_query(lambda callback_query: _has_private_library_callback(callback_query.data))
    async def handle_private_library_callback(callback_query: CallbackQuery) -> None:
        await handle_library_callback(
            callback_query=callback_query,
            session_factory=resolved_session_factory,
            collection_service_factory=resolved_service_factory,
        )

    return router


async def show_library_menu(
    *,
    message: Message,
    session_factory: AsyncSessionFactory,
    collection_service_factory: PrivateLibraryCollectionServiceFactory,
) -> None:
    user = await _resolve_user_for_message(message, session_factory=session_factory)
    if user is None:
        await message.answer(_unlinked_message())
        return

    async with session_factory() as session:
        service = collection_service_factory(session)
        try:
            library = await service.get_meme_library(user_id=user.id)
        except (CollectionServiceError, MemeNotFoundError, UserNotFoundError) as exc:
            await message.answer(_service_error_message(exc))
            return

    await message.answer(_render_menu(library), reply_markup=_menu_keyboard())


async def show_favorites(
    *,
    message: Message,
    page: int,
    session_factory: AsyncSessionFactory,
    collection_service_factory: PrivateLibraryCollectionServiceFactory,
) -> None:
    await _send_listing(
        message=message,
        section="favorites",
        page=page,
        session_factory=session_factory,
        collection_service_factory=collection_service_factory,
    )


async def show_pins(
    *,
    message: Message,
    page: int,
    session_factory: AsyncSessionFactory,
    collection_service_factory: PrivateLibraryCollectionServiceFactory,
) -> None:
    await _send_listing(
        message=message,
        section="pins",
        page=page,
        session_factory=session_factory,
        collection_service_factory=collection_service_factory,
    )


async def show_active_collection(
    *,
    message: Message,
    page: int,
    session_factory: AsyncSessionFactory,
    collection_service_factory: PrivateLibraryCollectionServiceFactory,
) -> None:
    await _send_listing(
        message=message,
        section="active",
        page=page,
        session_factory=session_factory,
        collection_service_factory=collection_service_factory,
    )


async def show_collections(
    *,
    message: Message,
    page: int,
    session_factory: AsyncSessionFactory,
    collection_service_factory: PrivateLibraryCollectionServiceFactory,
) -> None:
    await _send_listing(
        message=message,
        section="collections",
        page=page,
        session_factory=session_factory,
        collection_service_factory=collection_service_factory,
    )


async def create_collection(
    *,
    message: Message,
    title: str,
    session_factory: AsyncSessionFactory,
    collection_service_factory: PrivateLibraryCollectionServiceFactory,
) -> None:
    if not title:
        await message.answer("Используйте: /collection_create Название коллекции")
        return

    user = await _resolve_user_for_message(message, session_factory=session_factory)
    if user is None:
        await message.answer(_unlinked_message())
        return

    async with session_factory() as session:
        service = collection_service_factory(session)
        try:
            collection = await service.create_custom_collection(owner_user_id=user.id, title=title)
        except (CollectionServiceError, MemeNotFoundError, UserNotFoundError) as exc:
            await message.answer(_service_error_message(exc))
            return

    await message.answer(
        f"Коллекция создана: {collection.title}",
        reply_markup=_single_button_keyboard("Коллекции", _callback("c", 0)),
    )


async def handle_library_callback(
    *,
    callback_query: CallbackQuery,
    session_factory: AsyncSessionFactory,
    collection_service_factory: PrivateLibraryCollectionServiceFactory,
) -> None:
    user = await _resolve_user_for_callback(callback_query, session_factory=session_factory)
    if user is None:
        await callback_query.answer(_unlinked_message(), show_alert=True)
        return

    parsed = _parse_callback(callback_query.data)
    if parsed is None:
        await callback_query.answer("Эта кнопка устарела.", show_alert=True)
        return

    action, args = parsed
    async with session_factory() as session:
        service = collection_service_factory(session)
        try:
            text, keyboard = await _handle_callback_action(service=service, user=user, action=action, args=args)
        except (CollectionServiceError, MemeNotFoundError, UserNotFoundError) as exc:
            logger.info("Telegram private library callback rejected: %s", exc)
            await callback_query.answer(_service_error_message(exc), show_alert=True)
            return

    callback_answered = await _edit_or_answer_callback(callback_query, text=text, reply_markup=keyboard)
    if not callback_answered:
        await callback_query.answer()


async def _send_listing(
    *,
    message: Message,
    section: str,
    page: int,
    session_factory: AsyncSessionFactory,
    collection_service_factory: PrivateLibraryCollectionServiceFactory,
) -> None:
    user = await _resolve_user_for_message(message, session_factory=session_factory)
    if user is None:
        await message.answer(_unlinked_message())
        return

    async with session_factory() as session:
        service = collection_service_factory(session)
        try:
            text, keyboard = await _render_section(service=service, user_id=user.id, section=section, page=page)
        except (CollectionServiceError, MemeNotFoundError, UserNotFoundError) as exc:
            await message.answer(_service_error_message(exc))
            return

    await message.answer(text, reply_markup=keyboard)


async def _handle_callback_action(
    *,
    service: PrivateLibraryCollectionService,
    user: User,
    action: str,
    args: list[str],
) -> tuple[str, InlineKeyboardMarkup]:
    if action == "m":
        library = await service.get_meme_library(user_id=user.id)
        return _render_menu(library), _menu_keyboard()
    if action in {"f", "p", "a", "c"}:
        return await _render_section(
            service=service,
            user_id=user.id,
            section=_section_for_action(action),
            page=_parse_page(args),
        )
    if action == "uf":
        meme_id = _parse_uuid_arg(args, 0)
        page = _parse_page(args[1:])
        if meme_id is None:
            return _stale_button_response()
        _ = await service.unfavorite_meme(user_id=user.id, meme_id=meme_id)
        return await _render_section(service=service, user_id=user.id, section="favorites", page=page)
    if action == "rs":
        meme_id = _parse_uuid_arg(args, 0)
        page = _parse_page(args[1:])
        if meme_id is None:
            return _stale_button_response()
        _ = await service.remove_meme_from_active_collection(user_id=user.id, meme_id=meme_id)
        return await _render_section(service=service, user_id=user.id, section="active", page=page)
    if action == "pn":
        meme_id = _parse_uuid_arg(args, 0)
        section, page = _parse_return_target(args[1:])
        if meme_id is None:
            return _stale_button_response()
        _ = await service.pin_meme(user_id=user.id, meme_id=meme_id)
        return await _render_section(service=service, user_id=user.id, section=section, page=page)
    if action == "up":
        meme_id = _parse_uuid_arg(args, 0)
        section, page = _parse_return_target(args[1:])
        if meme_id is None:
            return _stale_button_response()
        _ = await service.unpin_meme(user_id=user.id, meme_id=meme_id)
        return await _render_section(service=service, user_id=user.id, section=section, page=page)
    if action in {"pu", "pd"}:
        meme_id = _parse_uuid_arg(args, 0)
        page = _parse_page(args[1:])
        if meme_id is None:
            return _stale_button_response()
        await _move_pin(service=service, user_id=user.id, meme_id=meme_id, direction=-1 if action == "pu" else 1)
        return await _render_section(service=service, user_id=user.id, section="pins", page=page)
    if action == "as":
        collection_id = _parse_uuid_arg(args, 0)
        page = _parse_page(args[1:])
        if collection_id is None:
            return _stale_button_response()
        _ = await service.update_active_save_collection(user_id=user.id, collection_id=collection_id)
        return await _render_section(service=service, user_id=user.id, section="collections", page=page)
    if action == "cd":
        collection_id = _parse_uuid_arg(args, 0)
        page = _parse_page(args[1:])
        if collection_id is None:
            return _stale_button_response()
        collections = await service.list_collections_for_user(user_id=user.id)
        collection = next((item for item in collections if item.id == collection_id), None)
        if collection is None:
            return "Коллекция не найдена.", _single_button_keyboard("Коллекции", _callback("c", page))
        if collection.kind is CollectionKind.FAVORITES:
            return "Favorites нельзя удалить.", _collections_keyboard(collections, active_collection_id=None, page=page)
        _ = await service.delete_custom_collection(collection_id=collection_id, user_id=user.id)
        return await _render_section(service=service, user_id=user.id, section="collections", page=page)
    return _stale_button_response()


async def _render_section(
    *,
    service: PrivateLibraryCollectionService,
    user_id: uuid.UUID,
    section: str,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    if section == "favorites":
        favorites = await service.list_favorite_memes(user_id=user_id)
        return _render_favorites(favorites, page=page)
    if section == "pins":
        pins = await service.list_pinned_memes(user_id=user_id)
        return _render_pins(pins, page=page)
    if section == "active":
        library = await service.get_meme_library(user_id=user_id)
        active_collection = library.active_save_collection
        saves = [] if active_collection is None else await service.list_collection_memes(
            collection_id=active_collection.id,
            user_id=user_id,
        )
        return _render_active(
            active_title=active_collection.title if active_collection else None,
            saves=saves,
            page=page,
        )
    if section == "collections":
        library = await service.get_meme_library(user_id=user_id)
        collections = await service.list_collections_for_user(user_id=user_id)
        active_collection_id = library.active_save_collection.id if library.active_save_collection else None
        return _render_collections(collections, active_collection_id=active_collection_id, page=page)
    return _stale_button_response()


async def _move_pin(
    *,
    service: PrivateLibraryCollectionService,
    user_id: uuid.UUID,
    meme_id: uuid.UUID,
    direction: int,
) -> None:
    pins = await service.list_pinned_memes(user_id=user_id)
    meme_ids = [pin.meme_id for pin in pins]
    try:
        index = meme_ids.index(meme_id)
    except ValueError:
        return
    target_index = index + direction
    if target_index < 0 or target_index >= len(meme_ids):
        return
    meme_ids[index], meme_ids[target_index] = meme_ids[target_index], meme_ids[index]
    _ = await service.reorder_pins(user_id=user_id, meme_ids=meme_ids)


async def _resolve_user_for_message(
    message: Message,
    *,
    session_factory: AsyncSessionFactory,
) -> User | None:
    telegram_user = message.from_user
    if telegram_user is None or telegram_user.id <= 0:
        return None
    async with session_factory() as session:
        return await _resolve_active_full_linked_user(session, telegram_user_id=telegram_user.id)


async def _resolve_user_for_callback(
    callback_query: CallbackQuery,
    *,
    session_factory: AsyncSessionFactory,
) -> User | None:
    if callback_query.from_user.id <= 0:
        return None
    async with session_factory() as session:
        return await _resolve_active_full_linked_user(session, telegram_user_id=callback_query.from_user.id)


async def _resolve_active_full_linked_user(session: AsyncSession, *, telegram_user_id: int) -> User | None:
    user: User | None = await session.scalar(
        select(User).where(
            User.telegram_id == telegram_user_id,
            User.account_type == AccountType.FULL,
            User.status == AccountStatus.ACTIVE,
        )
    )
    return user


def _render_menu(library: MemeLibraryRead) -> str:
    active = library.active_save_collection.title if library.active_save_collection else "не выбрана"
    return (
        "Библиотека MemeXpert\n"
        f"Favorites: {len(library.favorites)}\n"
        f"Pins: {len(library.pinned_memes)}\n"
        f"Collections: {len(library.collections)}\n"
        f"Active Save Collection: {active}\n\n"
        "Команды: /favorites, /pins, /active, /collections, /collection_create <название>"
    )


def _render_favorites(items: list[CollectionMemeRead], *, page: int) -> tuple[str, InlineKeyboardMarkup]:
    page_items, normalized_page, total_pages = _page(items, page=page)
    lines = [f"Favorites ({len(items)})", _page_label(normalized_page, total_pages)]
    if not page_items:
        lines.append("Пока пусто.")
    else:
        lines.extend(f"{index}. {_short_id(item.meme_id)}" for index, item in _numbered(page_items, normalized_page))
    return "\n".join(lines), _meme_list_keyboard(
        page_items,
        section="favorites",
        page=normalized_page,
        total_pages=total_pages,
    )


def _render_pins(items: list[PinnedMemeRead], *, page: int) -> tuple[str, InlineKeyboardMarkup]:
    ordered = sorted(items, key=lambda item: item.position)
    page_items, normalized_page, total_pages = _page(ordered, page=page)
    lines = [f"Pins ({len(items)}/20)", _page_label(normalized_page, total_pages)]
    if not page_items:
        lines.append("Пока нет закреплённых мемов.")
    else:
        lines.extend(f"{item.position}. {_short_id(item.meme_id)}" for item in page_items)
    return "\n".join(lines), _pins_keyboard(page_items, page=normalized_page, total_pages=total_pages)


def _render_active(
    *,
    active_title: str | None,
    saves: list[CollectionMemeRead],
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    page_items, normalized_page, total_pages = _page(saves, page=page)
    title = active_title or "не выбрана"
    lines = [
        f"Active Save Collection: {title}",
        f"Saved memes: {len(saves)}",
        _page_label(normalized_page, total_pages),
    ]
    if not page_items:
        lines.append("В активной коллекции пока нет мемов.")
    else:
        lines.extend(f"{index}. {_short_id(item.meme_id)}" for index, item in _numbered(page_items, normalized_page))
    return "\n".join(lines), _active_keyboard(page_items, page=normalized_page, total_pages=total_pages)


def _render_collections(
    items: list[CollectionRead],
    *,
    active_collection_id: uuid.UUID | None,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    page_items, normalized_page, total_pages = _page(items, page=page)
    lines = [f"Collections ({len(items)})", _page_label(normalized_page, total_pages)]
    if not page_items:
        lines.append("Пока нет коллекций. Создайте: /collection_create Название")
    else:
        for index, item in _numbered(page_items, normalized_page):
            marker = "*" if item.id == active_collection_id else " "
            lines.append(f"{index}. {marker} {item.title} ({item.kind.value})")
    lines.append("* = active save collection")
    return "\n".join(lines), _collections_keyboard(
        page_items,
        active_collection_id=active_collection_id,
        page=normalized_page,
        total_pages=total_pages,
    )


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Favorites", callback_data=_callback("f", 0))],
            [InlineKeyboardButton(text="Pins", callback_data=_callback("p", 0))],
            [InlineKeyboardButton(text="Active Save Collection", callback_data=_callback("a", 0))],
            [InlineKeyboardButton(text="Collections", callback_data=_callback("c", 0))],
        ]
    )


def _meme_list_keyboard(
    items: list[CollectionMemeRead],
    *,
    section: str,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        target = _return_target(section, page)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Remove {_short_id(item.meme_id)}",
                    callback_data=_callback("uf", item.meme_id.hex, page),
                ),
                InlineKeyboardButton(text="Pin", callback_data=_callback("pn", item.meme_id.hex, target)),
            ]
        )
    rows.extend(_navigation_rows(section, page=page, total_pages=total_pages))
    rows.append([InlineKeyboardButton(text="Menu", callback_data=_callback("m"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pins_keyboard(items: list[PinnedMemeRead], *, page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        rows.append(
            [
                InlineKeyboardButton(text="Up", callback_data=_callback("pu", item.meme_id.hex, page)),
                InlineKeyboardButton(text="Down", callback_data=_callback("pd", item.meme_id.hex, page)),
                InlineKeyboardButton(
                    text="Unpin",
                    callback_data=_callback("up", item.meme_id.hex, _return_target("pins", page)),
                ),
            ]
        )
    rows.extend(_navigation_rows("pins", page=page, total_pages=total_pages))
    rows.append([InlineKeyboardButton(text="Menu", callback_data=_callback("m"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _active_keyboard(items: list[CollectionMemeRead], *, page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Remove {_short_id(item.meme_id)}",
                    callback_data=_callback("rs", item.meme_id.hex, page),
                ),
                InlineKeyboardButton(
                    text="Pin",
                    callback_data=_callback("pn", item.meme_id.hex, _return_target("active", page)),
                ),
            ]
        )
    rows.extend(_navigation_rows("active", page=page, total_pages=total_pages))
    rows.append([InlineKeyboardButton(text="Collections", callback_data=_callback("c", 0))])
    rows.append([InlineKeyboardButton(text="Menu", callback_data=_callback("m"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _collections_keyboard(
    items: list[CollectionRead],
    *,
    active_collection_id: uuid.UUID | None,
    page: int,
    total_pages: int = 1,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        row = []
        if item.id != active_collection_id:
            row.append(
                InlineKeyboardButton(
                    text=f"Set active: {item.title[:20]}",
                    callback_data=_callback("as", item.id.hex, page),
                )
            )
        if item.kind is not CollectionKind.FAVORITES:
            row.append(
                InlineKeyboardButton(
                    text=f"Delete: {item.title[:20]}",
                    callback_data=_callback("cd", item.id.hex, page),
                )
            )
        if row:
            rows.append(row)
    rows.extend(_navigation_rows("collections", page=page, total_pages=total_pages))
    rows.append([InlineKeyboardButton(text="Menu", callback_data=_callback("m"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _single_button_keyboard(text: str, callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=callback_data)]])


def _navigation_rows(section: str, *, page: int, total_pages: int) -> list[list[InlineKeyboardButton]]:
    action = _action_for_section(section)
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="Prev", callback_data=_callback(action, page - 1)))
    if page + 1 < total_pages:
        row.append(InlineKeyboardButton(text="Next", callback_data=_callback(action, page + 1)))
    return [row] if row else []


async def _edit_or_answer_callback(
    callback_query: CallbackQuery,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> bool:
    message: MaybeInaccessibleMessage | None = callback_query.message
    if message is not None and hasattr(message, "edit_text"):
        await message.edit_text(text, reply_markup=reply_markup)
        return False
    await callback_query.answer(_callback_alert_text(text), show_alert=True)
    return True


def _callback_alert_text(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else "Готово."
    if len(first_line) <= 190:
        return first_line
    return f"{first_line[:187]}..."


def _page[T](items: list[T], *, page: int) -> tuple[list[T], int, int]:
    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    normalized_page = min(max(page, 0), total_pages - 1)
    start = normalized_page * PAGE_SIZE
    return items[start : start + PAGE_SIZE], normalized_page, total_pages


def _numbered[T](items: list[T], page: int) -> list[tuple[int, T]]:
    return list(enumerate(items, start=page * PAGE_SIZE + 1))


def _page_label(page: int, total_pages: int) -> str:
    return f"Page {page + 1}/{total_pages}"


def _short_id(value: uuid.UUID) -> str:
    return value.hex[:8]


def _callback(action: str, *parts: object) -> str:
    return ":".join([CALLBACK_PREFIX, action, *(str(part) for part in parts)])


def _has_private_library_callback(data: str | None) -> bool:
    return data is not None and data.startswith(f"{CALLBACK_PREFIX}:")


def _parse_callback(data: str | None) -> tuple[str, list[str]] | None:
    if not _has_private_library_callback(data):
        return None
    assert data is not None
    parts = data.split(":")
    if len(parts) < 2:
        return None
    return parts[1], parts[2:]


def _parse_page(args: list[str]) -> int:
    if not args:
        return 0
    try:
        return max(0, int(args[0]))
    except ValueError:
        return 0


def _parse_uuid_arg(args: list[str], index: int) -> uuid.UUID | None:
    if index >= len(args):
        return None
    try:
        return uuid.UUID(hex=args[index])
    except ValueError:
        return None


def _return_target(section: str, page: int) -> str:
    return f"{_action_for_section(section)}{page}"


def _parse_return_target(args: list[str]) -> tuple[str, int]:
    if not args:
        return "favorites", 0
    value = args[0]
    action = value[:1]
    try:
        page = max(0, int(value[1:] or "0"))
    except ValueError:
        page = 0
    return _section_for_action(action), page


def _section_for_action(action: str) -> str:
    return {
        "f": "favorites",
        "p": "pins",
        "a": "active",
        "c": "collections",
    }.get(action, "favorites")


def _action_for_section(section: str) -> str:
    return {
        "favorites": "f",
        "pins": "p",
        "active": "a",
        "collections": "c",
    }.get(section, "m")


def _stale_button_response() -> tuple[str, InlineKeyboardMarkup]:
    return "Эта кнопка устарела. Откройте меню заново.", _menu_keyboard()


def _unlinked_message() -> str:
    return "Сначала привяжите Telegram к полному аккаунту MemeXpert, затем откройте /library."


def _service_error_message(exc: Exception) -> str:
    if isinstance(
        exc,
        (
            CollectionWriteAccessError,
            GuestCollectionAccessError,
            InvalidCollectionTitleError,
            InvalidPinnedMemeOrderError,
            PinLimitExceededError,
        ),
    ):
        return str(exc)
    if isinstance(exc, MemeNotFoundError):
        return "Мем не найден или недоступен."
    if isinstance(exc, UserNotFoundError):
        return "Аккаунт MemeXpert не найден. Перепривяжите Telegram и попробуйте снова."
    return "Действие с библиотекой сейчас недоступно. Попробуйте позже."


__all__ = [
    "PrivateLibraryCollectionServiceFactory",
    "build_private_library_router",
    "handle_library_callback",
    "show_library_menu",
]
