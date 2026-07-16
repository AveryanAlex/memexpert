"""Deterministic compiler and publish validator for curated synonym groups."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal, cast

from memexpert.models.enums import SearchSynonymLocale

MAX_TERM_LENGTH: Final = 255
MAX_KEY_TOKENS: Final = 3
MAX_TARGETS_PER_KEY: Final = 50
MAX_TARGET_WORDS_PER_KEY: Final = 100
SEARCH_SYNONYM_COMPILER_VERSION: Final = "meili_synonyms_v1"

_WHITESPACE_RE: Final = re.compile(r"\s+")
_WORD_RE: Final = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)

type SynonymIssueLevel = Literal["error", "warning"]
type CompiledSynonymMap = dict[str, list[str]]


@dataclass(frozen=True, slots=True)
class SynonymValidationIssue:
    """One deterministic source validation result."""

    level: SynonymIssueLevel
    code: str
    message: str
    line_number: int | None = None
    term: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "line_number": self.line_number,
            "term": self.term,
        }


@dataclass(frozen=True, slots=True)
class SearchSynonymCompileResult:
    """Canonical compilation output safe to persist in a draft revision."""

    compiled_synonyms: CompiledSynonymMap
    compiler_version: str
    compiled_hash: str
    validation: dict[str, object]
    stats: dict[str, int]

    @property
    def valid(self) -> bool:
        return bool(self.validation["valid"])

    @property
    def issues(self) -> list[dict[str, object]]:
        issues = self.validation["issues"]
        if not isinstance(issues, list):  # pragma: no cover - constructed locally.
            raise TypeError("Compiler validation issues must be a list.")
        return cast("list[dict[str, object]]", issues)


def normalize_synonym_term(value: str) -> str:
    """Apply the same stable source normalization to every authored term."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = unicodedata.normalize("NFKC", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def count_synonym_tokens(value: str) -> int:
    """Approximate Meilisearch word tokenization for the three-token key cap."""

    return len(_WORD_RE.findall(value))


def canonicalize_synonym_map(synonyms: dict[str, list[str]]) -> CompiledSynonymMap:
    """Sort and deduplicate a complete synonym map for stable hashing."""

    return {
        key: sorted(set(synonyms[key]))
        for key in sorted(synonyms)
    }


def hash_synonym_map(synonyms: dict[str, list[str]]) -> tuple[str, int]:
    """Return SHA-256 and compact UTF-8 payload size for a canonical map."""

    canonical = canonicalize_synonym_map(synonyms)
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest(), len(payload)


def hash_compiled_synonym_snapshot(
    synonyms: dict[str, list[str]],
    *,
    compiler_version: str = SEARCH_SYNONYM_COMPILER_VERSION,
) -> str:
    """Hash a revision snapshot with the compiler contract that produced it."""

    payload = json.dumps(
        {
            "compiler_version": compiler_version,
            "synonyms": canonicalize_synonym_map(synonyms),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def merge_synonym_maps(*maps: dict[str, list[str]]) -> CompiledSynonymMap:
    """Merge locale snapshots into one complete Meilisearch settings map."""

    merged: dict[str, set[str]] = {}
    for synonyms in maps:
        for key, targets in synonyms.items():
            merged.setdefault(key, set()).update(targets)
    return {key: sorted(merged[key]) for key in sorted(merged)}


def compile_search_synonyms(
    source_text: str,
    *,
    locale: SearchSynonymLocale,
) -> SearchSynonymCompileResult:
    """Compile newline/comma mutual groups while retaining all draft issues."""

    issues: list[SynonymValidationIssue] = []
    seen_terms: dict[str, int] = {}
    groups: list[tuple[int, list[tuple[str, int]]]] = []
    nonblank_line_count = 0
    term_count = 0
    target_only_term_count = 0

    for line_number, raw_line in enumerate(source_text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        nonblank_line_count += 1
        raw_terms = raw_line.split(",")
        if len(raw_terms) < 2:
            issues.append(
                SynonymValidationIssue(
                    level="error",
                    code="group_requires_two_terms",
                    message="Each nonblank line must contain at least two comma-separated terms.",
                    line_number=line_number,
                )
            )

        valid_terms: list[tuple[str, int]] = []
        for raw_term in raw_terms:
            stripped = raw_term.strip()
            if not stripped:
                issues.append(
                    SynonymValidationIssue(
                        level="error",
                        code="empty_term",
                        message="Synonym terms cannot be empty.",
                        line_number=line_number,
                    )
                )
                continue

            normalized = normalize_synonym_term(stripped)
            term_count += 1
            if not normalized:
                issues.append(
                    SynonymValidationIssue(
                        level="error",
                        code="empty_normalized_term",
                        message="The term is empty after Unicode and whitespace normalization.",
                        line_number=line_number,
                        term=stripped,
                    )
                )
                continue
            if len(normalized) > MAX_TERM_LENGTH:
                issues.append(
                    SynonymValidationIssue(
                        level="error",
                        code="term_too_long",
                        message=f"Terms must be at most {MAX_TERM_LENGTH} characters after normalization.",
                        line_number=line_number,
                        term=normalized,
                    )
                )
                continue
            if _contains_disallowed_script(normalized, locale=locale):
                disallowed = "Cyrillic" if locale is SearchSynonymLocale.EN else "Latin"
                issues.append(
                    SynonymValidationIssue(
                        level="error",
                        code="wrong_script",
                        message=f"The {locale.value.upper()} catalog cannot contain {disallowed} letters.",
                        line_number=line_number,
                        term=normalized,
                    )
                )
                continue

            token_count = count_synonym_tokens(normalized)
            if token_count == 0:
                issues.append(
                    SynonymValidationIssue(
                        level="error",
                        code="term_requires_word",
                        message="Each term must contain at least one word token.",
                        line_number=line_number,
                        term=normalized,
                    )
                )
                continue

            previous_line = seen_terms.get(normalized)
            if previous_line is not None:
                issues.append(
                    SynonymValidationIssue(
                        level="error",
                        code="duplicate_term",
                        message=f"Normalized terms must be globally unique; first used on line {previous_line}.",
                        line_number=line_number,
                        term=normalized,
                    )
                )
                continue

            seen_terms[normalized] = line_number
            valid_terms.append((normalized, token_count))

        if len(valid_terms) >= 2:
            groups.append((line_number, valid_terms))

    compiled: dict[str, list[str]] = {}
    for line_number, terms in groups:
        eligible_keys = [(term, token_count) for term, token_count in terms if token_count <= MAX_KEY_TOKENS]
        target_only_terms = [(term, token_count) for term, token_count in terms if token_count > MAX_KEY_TOKENS]
        target_only_term_count += len(target_only_terms)
        for term, token_count in target_only_terms:
            issues.append(
                SynonymValidationIssue(
                    level="warning",
                    code="target_only_term",
                    message=(
                        f"Meilisearch 1.47 only activates keys with at most {MAX_KEY_TOKENS} tokens; "
                        f"this {token_count}-token term will be a target only."
                    ),
                    line_number=line_number,
                    term=term,
                )
            )
        if not eligible_keys:
            issues.append(
                SynonymValidationIssue(
                    level="warning",
                    code="inactive_group_no_eligible_key",
                    message=(
                        f"This group has no term with at most {MAX_KEY_TOKENS} tokens and will be "
                        "inactive in Meilisearch 1.47."
                    ),
                    line_number=line_number,
                )
            )
            continue

        for key, _key_token_count in eligible_keys:
            targets = sorted(term for term, _token_count in terms if term != key)
            if len(targets) > MAX_TARGETS_PER_KEY:
                issues.append(
                    SynonymValidationIssue(
                        level="error",
                        code="too_many_targets",
                        message=f"A synonym key can have at most {MAX_TARGETS_PER_KEY} targets.",
                        line_number=line_number,
                        term=key,
                    )
                )
            target_word_count = sum(count_synonym_tokens(target) for target in targets)
            if target_word_count > MAX_TARGET_WORDS_PER_KEY:
                issues.append(
                    SynonymValidationIssue(
                        level="error",
                        code="too_many_target_words",
                        message=(
                            "A synonym key can have at most "
                            f"{MAX_TARGET_WORDS_PER_KEY} words across all targets."
                        ),
                        line_number=line_number,
                        term=key,
                    )
                )
            compiled[key] = targets

    canonical = canonicalize_synonym_map(compiled)
    if not canonical:
        issues.append(
            SynonymValidationIssue(
                level="error",
                code="catalog_requires_compiled_key",
                message="A published synonym catalog must compile at least one active key.",
            )
        )
    _map_hash, payload_bytes = hash_synonym_map(canonical)
    compiled_hash = hash_compiled_synonym_snapshot(canonical)
    ordered_issues = sorted(
        issues,
        key=lambda issue: (
            issue.line_number if issue.line_number is not None else 0,
            issue.term or "",
            issue.level,
            issue.code,
        ),
    )
    error_count = sum(issue.level == "error" for issue in ordered_issues)
    warning_count = sum(issue.level == "warning" for issue in ordered_issues)
    stats = {
        "group_count": nonblank_line_count,
        "term_count": term_count,
        "compiled_key_count": len(canonical),
        "edge_count": sum(len(targets) for targets in canonical.values()),
        "target_only_term_count": target_only_term_count,
        "payload_bytes": payload_bytes,
        "error_count": error_count,
        "warning_count": warning_count,
    }
    validation: dict[str, object] = {
        "valid": error_count == 0,
        "issues": [issue.as_dict() for issue in ordered_issues],
    }
    return SearchSynonymCompileResult(
        compiled_synonyms=canonical,
        compiler_version=SEARCH_SYNONYM_COMPILER_VERSION,
        compiled_hash=compiled_hash,
        validation=validation,
        stats=stats,
    )


def _contains_disallowed_script(value: str, *, locale: SearchSynonymLocale) -> bool:
    disallowed_script = "CYRILLIC" if locale is SearchSynonymLocale.EN else "LATIN"
    return any(disallowed_script in unicodedata.name(character, "") for character in value)


__all__ = [
    "CompiledSynonymMap",
    "MAX_KEY_TOKENS",
    "MAX_TARGETS_PER_KEY",
    "MAX_TARGET_WORDS_PER_KEY",
    "MAX_TERM_LENGTH",
    "SEARCH_SYNONYM_COMPILER_VERSION",
    "SearchSynonymCompileResult",
    "SynonymValidationIssue",
    "canonicalize_synonym_map",
    "compile_search_synonyms",
    "count_synonym_tokens",
    "hash_synonym_map",
    "hash_compiled_synonym_snapshot",
    "merge_synonym_maps",
    "normalize_synonym_term",
]
