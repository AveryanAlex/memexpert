"""Conservative shared query-time prefilters for search index adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast


class SearchIndexPrefilterScope(StrEnum):
    """Safe access scopes the search indexes can conservatively prefilter."""

    NONE = "none"
    PUBLIC = "public"
    PRIVATE = "private"
    ALL = "all"
    COLLECTIONS = "collections"


@dataclass(frozen=True, slots=True)
class SearchIndexPrefilter:
    """Portable filter description shared by Meilisearch and Qdrant adapters.

    The filter is intentionally conservative. It can reduce obviously irrelevant
    candidates early, but PostgreSQL remains the final access authority.
    """

    scope: SearchIndexPrefilterScope
    search_index_algorithm_version: str
    viewer_user_id: str | None = None
    collection_ids: tuple[str, ...] = ()
    media_type: str | None = None
    language: str | None = None
    include_nsfw: bool = False
    tags: tuple[str, ...] = ()

    def to_meilisearch_filter(self) -> str | None:
        """Render the prefilter as a Meilisearch filter expression."""

        clauses = [
            f'search_index_algorithm_version = {self._quote(self.search_index_algorithm_version)}',
        ]
        access_clause = self._meilisearch_access_clause()
        if access_clause is not None:
            clauses.append(access_clause)
        if self.collection_ids and self.scope is SearchIndexPrefilterScope.COLLECTIONS:
            collection_clause = " OR ".join(
                f'collection_ids = {self._quote(collection_id)}' for collection_id in self.collection_ids
            )
            clauses.append(f"({collection_clause})")
        if self.media_type is not None:
            clauses.append(f'media_type = {self._quote(self.media_type)}')
        if self.language is not None:
            clauses.append(f'language = {self._quote(self.language)}')
        if not self.include_nsfw:
            clauses.append("is_nsfw = false")
        for tag in self.tags:
            clauses.append(f'tags = {self._quote(tag)}')
        return " AND ".join(clauses) if clauses else None

    def to_qdrant_filter(self) -> object | None:
        """Render the prefilter as a Qdrant ``query_filter`` object."""

        from qdrant_client.http.models import FieldCondition, Filter, MatchAny, MatchValue

        must_conditions: list[Any] = [
            FieldCondition(
                key="search_index_algorithm_version",
                match=MatchValue(value=self.search_index_algorithm_version),
            )
        ]
        access_filter = self._qdrant_access_filter()
        if access_filter is not None:
            must_conditions.append(access_filter)
        if self.collection_ids and self.scope is SearchIndexPrefilterScope.COLLECTIONS:
            must_conditions.append(
                FieldCondition(
                    key="collection_ids",
                    match=MatchAny(any=list(self.collection_ids)),
                )
            )
        if self.media_type is not None:
            must_conditions.append(FieldCondition(key="media_type", match=MatchValue(value=self.media_type)))
        if self.language is not None:
            must_conditions.append(FieldCondition(key="language", match=MatchValue(value=self.language)))
        if not self.include_nsfw:
            must_conditions.append(FieldCondition(key="is_nsfw", match=MatchValue(value=False)))
        for tag in self.tags:
            must_conditions.append(FieldCondition(key="tags", match=MatchAny(any=[tag])))
        return cast("object", Filter(must=must_conditions))

    def _meilisearch_access_clause(self) -> str | None:
        if self.scope is SearchIndexPrefilterScope.NONE:
            return 'id = "__no_results__"'
        if self.scope is SearchIndexPrefilterScope.PUBLIC:
            return "is_public = true"
        access_clause = self._meilisearch_viewer_access_clause()
        if self.scope is SearchIndexPrefilterScope.PRIVATE:
            return access_clause
        if self.scope is SearchIndexPrefilterScope.ALL:
            if access_clause is None:
                return "is_public = true"
            return f"(is_public = true OR {access_clause})"
        return None

    def _meilisearch_viewer_access_clause(self) -> str | None:
        if self.viewer_user_id is None:
            return None
        viewer = self._quote(self.viewer_user_id)
        return (
            f"(author_user_id = {viewer} OR collection_owner_user_ids = {viewer} "
            f"OR collection_member_user_ids = {viewer})"
        )

    def _qdrant_access_filter(self) -> object | None:
        from qdrant_client.http.models import FieldCondition, Filter, MatchAny, MatchValue

        if self.scope is SearchIndexPrefilterScope.NONE:
            return FieldCondition(key="search_index_algorithm_version", match=MatchValue(value="__no_results__"))
        if self.scope is SearchIndexPrefilterScope.PUBLIC:
            return FieldCondition(key="is_public", match=MatchValue(value=True))
        access_should: list[Any] = []
        if self.viewer_user_id is not None:
            access_should = [
                FieldCondition(key="author_user_id", match=MatchValue(value=self.viewer_user_id)),
                FieldCondition(key="collection_owner_user_ids", match=MatchAny(any=[self.viewer_user_id])),
                FieldCondition(key="collection_member_user_ids", match=MatchAny(any=[self.viewer_user_id])),
            ]
        if self.scope is SearchIndexPrefilterScope.PRIVATE:
            return cast("object", Filter(should=access_should)) if access_should else None
        if self.scope is SearchIndexPrefilterScope.ALL:
            if not access_should:
                return FieldCondition(key="is_public", match=MatchValue(value=True))
            return cast(
                "object",
                Filter(
                    should=[
                        FieldCondition(key="is_public", match=MatchValue(value=True)),
                        Filter(should=access_should),
                    ]
                ),
            )
        return None

    @staticmethod
    def _quote(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'


__all__ = ["SearchIndexPrefilter", "SearchIndexPrefilterScope"]
