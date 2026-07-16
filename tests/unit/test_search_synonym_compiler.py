"""Focused contract tests for the deterministic synonym compiler."""

from __future__ import annotations

from pathlib import Path

import pytest

from memexpert.models.enums import SearchSynonymLocale
from memexpert.services.search_synonym_compiler import (
    SEARCH_SYNONYM_COMPILER_VERSION,
    compile_search_synonyms,
    hash_compiled_synonym_snapshot,
    normalize_synonym_term,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_compiler_normalizes_and_expands_mutual_groups_deterministically() -> None:
    source = " Frog , TOAD \n\nnew   york,NYC\n"

    result = compile_search_synonyms(source, locale=SearchSynonymLocale.EN)
    reordered = compile_search_synonyms("NYC,new york\nTOAD,Frog\n", locale=SearchSynonymLocale.EN)

    assert result.valid is True
    assert result.compiler_version == SEARCH_SYNONYM_COMPILER_VERSION
    assert result.compiled_synonyms == {
        "frog": ["toad"],
        "new york": ["nyc"],
        "nyc": ["new york"],
        "toad": ["frog"],
    }
    assert result.compiled_hash == reordered.compiled_hash
    assert result.stats["group_count"] == 2
    assert result.stats["compiled_key_count"] == 4
    assert result.stats["edge_count"] == 4


def test_nfkc_casefold_and_global_term_uniqueness_are_enforced() -> None:
    assert normalize_synonym_term("  ＦＲＯＧ\t  Meme ") == "frog meme"

    result = compile_search_synonyms(
        "ＦＲＯＧ,toad\nfrog,amphibian\n",
        locale=SearchSynonymLocale.EN,
    )

    assert result.valid is False
    assert [issue["code"] for issue in result.issues if issue["level"] == "error"] == [
        "duplicate_term"
    ]
    assert result.issues[0]["line_number"] == 2
    assert result.issues[0]["term"] == "frog"


@pytest.mark.parametrize(
    ("locale", "source", "rejected_term"),
    [
        (SearchSynonymLocale.EN, "frog,лягушка", "лягушка"),
        (SearchSynonymLocale.RU, "жаба,frog", "frog"),
    ],
)
def test_compiler_rejects_the_other_catalog_script(
    locale: SearchSynonymLocale,
    source: str,
    rejected_term: str,
) -> None:
    result = compile_search_synonyms(source, locale=locale)

    assert result.valid is False
    assert any(
        issue["code"] == "wrong_script" and issue["term"] == rejected_term
        for issue in result.issues
    )


def test_long_terms_are_target_only_and_all_long_groups_are_inactive_warnings() -> None:
    result = compile_search_synonyms(
        "frog,this is a very long frog phrase\n"
        "one two three four,five six seven eight\n",
        locale=SearchSynonymLocale.EN,
    )

    assert result.valid is True
    assert result.compiled_synonyms == {"frog": ["this is a very long frog phrase"]}
    assert {issue["code"] for issue in result.issues} == {
        "inactive_group_no_eligible_key",
        "target_only_term",
    }
    assert result.stats["target_only_term_count"] == 3


def test_empty_or_only_inactive_catalog_cannot_be_published() -> None:
    empty = compile_search_synonyms("\n  \n", locale=SearchSynonymLocale.EN)
    inactive = compile_search_synonyms(
        "one two three four,five six seven eight",
        locale=SearchSynonymLocale.EN,
    )

    for result in (empty, inactive):
        assert result.valid is False
        assert result.stats["compiled_key_count"] == 0
        assert any(issue["code"] == "catalog_requires_compiled_key" for issue in result.issues)


def test_meilisearch_target_count_and_word_limits_are_publish_blocking() -> None:
    too_many_targets = compile_search_synonyms(
        ",".join(f"term{index}" for index in range(52)),
        locale=SearchSynonymLocale.EN,
    )
    too_many_words = compile_search_synonyms(
        "key," + " ".join("x" for _ in range(101)),
        locale=SearchSynonymLocale.EN,
    )

    assert too_many_targets.valid is False
    assert any(issue["code"] == "too_many_targets" for issue in too_many_targets.issues)
    assert too_many_words.valid is False
    assert any(issue["code"] == "too_many_target_words" for issue in too_many_words.issues)


def test_compiler_version_participates_in_revision_hash() -> None:
    synonyms = {"frog": ["toad"]}

    current_hash = hash_compiled_synonym_snapshot(synonyms)
    future_hash = hash_compiled_synonym_snapshot(synonyms, compiler_version="meili_synonyms_v2")

    assert current_hash != future_hash


@pytest.mark.parametrize(
    ("locale", "expected_groups", "inactive_groups"),
    [
        (SearchSynonymLocale.EN, 280, 27),
        (SearchSynonymLocale.RU, 185, 18),
    ],
)
def test_bundled_research_seeds_compile_without_publish_blocking_errors(
    locale: SearchSynonymLocale,
    expected_groups: int,
    inactive_groups: int,
) -> None:
    source = (
        REPOSITORY_ROOT
        / "docs"
        / "research"
        / f"meme-search-synonyms-{locale.value}.txt"
    ).read_text(encoding="utf-8")

    result = compile_search_synonyms(source, locale=locale)

    assert result.valid is True
    assert result.stats["group_count"] == expected_groups
    assert result.stats["compiled_key_count"] > 0
    assert sum(issue["code"] == "inactive_group_no_eligible_key" for issue in result.issues) == inactive_groups
    assert not [issue for issue in result.issues if issue["level"] == "error"]
