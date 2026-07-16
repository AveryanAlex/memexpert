# Meilisearch Synonym Behavior

Research date: 2026-07-16

This note documents how Meilisearch synonyms actually behave for MemeExpert's
search use case. It combines official Meilisearch documentation and source
review with isolated tests against Meilisearch `v1.47.0` and `v1.47.1`.
Production was not modified.

The accompanying research seeds are:

- [English meme aliases](meme-search-synonyms-en.txt)
- [Russian meme aliases](meme-search-synonyms-ru.txt)

These text files are bundled import seeds for the admin-managed PostgreSQL
catalog. Importing only replaces a draft; an operator must review validation and
publish explicitly before the scheduler can apply them.

## Executive summary

Meilisearch synonyms are query-side expansions. A synonym key must match exact
normalized query tokens; matching text in a document does not trigger an
expansion. A key may occur inside a longer query, so a one-word rule such as
`жаба -> лягушка` is useful in `жаба по средам`.

The strongest use cases are:

- spelling and orthographic variants;
- short, same-language lexical equivalents;
- short template names and aliases;
- two- or three-token catchphrase aliases.

Synonyms are not a replacement for:

- Russian lemmatization or stemming;
- typo correction or prefix completion;
- general multilingual translation;
- semantic similarity;
- robust handling of long or heavily punctuated catchphrases.

The implemented architecture uses PostgreSQL as the source of truth, with
versioned drafts and published revisions managed through an admin interface.
The singleton scheduler combines the immutable published snapshots into one
complete Meilisearch map, compares it with the current settings, and submits a
full replacement only when the canonical maps differ.

## Core behavior

| Question | Behavior in Meilisearch 1.47 |
|---|---|
| When are synonyms applied? | At query time. Documents are not expanded with synonym tokens. |
| What triggers a rule? | Exact normalized query tokens matching a configured key. |
| Must the key equal the whole query? | No. A key can be a contiguous segment inside a longer query. |
| Does document text trigger a rule? | No. Only query text performs the lookup. |
| Are rules directional? | Yes. Reverse mappings must be emitted explicitly. |
| Are rules transitive? | No. `A -> B` and `B -> C` do not make `A` search for `C`. |
| How are multiword values handled? | As ordered, adjacent phrases. |
| Do quoted query terms expand? | No. Quoted words and phrases do not use synonyms in 1.47. |
| Do prefixes activate a key? | No. A query prefix may match documents but does not activate the synonym. |
| Do typo matches activate a key? | No. The query token must match the normalized synonym key exactly. |
| Is Russian morphology handled? | No. Inflected forms need explicit entries or a separate normalization mechanism. |
| Is matching case-sensitive? | No. Keys and values are lowercased and Unicode-normalized. |
| Are `ё` and `е` equivalent? | No. Add both forms where both are common. |
| Is `PUT /settings/synonyms` additive? | No. It replaces the complete synonym map asynchronously. |

### Directionality is query-side

With this one-way map:

```json
{
  "phone": ["iphone"]
}
```

- query `phone` found documents containing `phone` and `iphone`;
- query `iphone` found documents containing `iphone`, but not `phone`.

The document containing `phone` did not itself cause reverse expansion. Mutual
groups therefore have to compile into an entry for every term:

```json
{
  "жаба": ["лягушка"],
  "лягушка": ["жаба"]
}
```

### A key can occur inside a longer query

The one-word mutual mapping above produced these results:

| Query | Alternate document | Observed Meilisearch ranking score |
|---|---|---:|
| `жаба` | `лягушка` | `0.9621` |
| `жаба по средам` | `лягушка по средам` | `0.9993` |
| `смешная жаба в среду` | `смешная лягушка в среду` | `0.9997` |

This is why lexical aliases such as
`жаба,жабы,лягушка,лягушки` are valuable. The unchanged words in a longer query
retain strong proximity and exactness signals, so replacing one token receives
almost the same score as a literal match.

### Whole-phrase aliases are weaker

With this mutual mapping:

```json
{
  "жаба по средам": ["среда мои чуваки"],
  "среда мои чуваки": ["жаба по средам"]
}
```

query `жаба по средам` returned:

- literal document `жаба по средам`: score `1.0`;
- synonym document `среда мои чуваки`: score about `0.2582`.

The synonym result is still retrieved, but Meilisearch penalizes it across
typo, proximity, and exactness ranking details. When the phrase appears inside
a longer query and surrounding words also match, the score improves. For
example, `смешная жаба по средам` matched `смешная среда мои чуваки`.

Exact terms are favored by the `exactness` ranking rule, but this does not
guarantee that every literal document ranks above every synonym document.
Earlier ranking rules such as proximity, attribute rank, and word position can
separate documents before `exactness` is evaluated.

MemeExpert currently does not request `_rankingScore` from Meilisearch, so the
hybrid service normally falls back to reciprocal result rank (`1 / rank`) for
the text component. Enabling real ranking scores later requires a separate
relevance evaluation because phrase-synonym scores can be much lower than their
position alone suggests.

## Version-specific key-length limit

Meilisearch 1.47 constructs synonym-key lookups for:

- one token;
- two adjacent tokens;
- three adjacent tokens.

Four-or-more-token keys do not activate in 1.47. This is visible in the 1.47
query graph implementation and is not prominently documented in the public
guide.

Long values can still be used as replacement phrases, subject to the per-key
limits below. A mutual group containing a four-word term is therefore not
truly mutual on 1.47: shorter keys may expand to the long value, but the long
term cannot reliably act as a reverse key.

The admin validator downgrades terms longer than three tokens to target-only
warnings while production remains on 1.47. A group containing only long terms
is inactive; the complete catalog still needs at least one eligible key before
it can be published.

## Tokenization, punctuation, and quotes

Case and Unicode normalization work as expected. Uppercase keys activate the
same mappings, and NFC/NFD variants normalize consistently. Cyrillic `ё` and
`е` remain distinct tokens.

Separator behavior matters for multiword keys and values:

- spaces, repeated spaces, newlines, and hyphens preserved adjacency in the
  tested cases;
- an internal comma or period followed by a space acted as a hard separator
  and prevented synonym-phrase recognition;
- surrounding punctuation such as parentheses or a trailing question mark did
  not prevent expansion;
- quoted query phrases did not expand.

The same issue applies on the document side of a multiword replacement. A
replacement `среда мои чуваки` matched documents using spaces, newlines, or
hyphens, but did not match `среда, мои чуваки` or `среда. мои чуваки` as a
synonym phrase. A normal literal query for `среда мои чуваки` could still match
those punctuated documents at lower proximity.

OCR punctuation therefore makes full catchphrase aliases less robust than
one-word lexical mappings.

## Prefixes, typos, and Russian morphology

Synonym lookup does not cascade from other search features:

- prefix `жаб` matched documents containing `жаба` or `жабы`, but did not
  expand to `лягушка`;
- typo-tolerant matches of a synonym key matched source documents but did not
  activate target synonyms;
- `жабы`, `жабу`, `жабой`, `лягушки`, and `лягушкой` do not derive from the
  singular keys systematically.

Charabia, Meilisearch's tokenizer, has no specialized Russian lemmatizer. It
normalizes and lowercases Cyrillic but does not perform grammatical stemming.

For the first version, include only the most valuable surface forms in curated
lexical groups. A stronger long-term design is a separate searchable alias or
lemma field generated by a Russian morphological analyzer, with the query
normalized through the same analyzer.

## Stop-word interaction

The default Meilisearch stop-word list is empty. MemeExpert does not currently
configure stop words at runtime.

Stop words inside synonym keys are dangerous. With `по` configured as a stop
word, the key `жаба по средам` normalizes to `жаба средам`. In 1.47 tests:

- query `жаба по средам` did not activate the alternate phrase because the
  skipped stop word still left a positional gap;
- query `жаба средам` did activate it.

English tests showed the same broader effect: with `it`, `is`, and `the` as
stop words, keys such as `it is wednesday` and `the frog` could activate from
the much shorter queries `wednesday` and `frog`.

The publish validator should reject configured stop-word tokens in synonym
keys. If Russian stop words are introduced later, the stop-word and synonym
settings must be designed and tested together.

## Settings updates and consistency

`PUT /indexes/{uid}/settings/synonyms`:

- returns HTTP `202` with an asynchronous `settingsUpdate` task;
- replaces the complete map rather than merging individual keys;
- removes omitted keys;
- clears all synonyms when sent `{}`.

Meilisearch 1.45 changed synonym updates so they no longer require a full
document reindex. They still build and publish synonym settings data through an
asynchronous task. In local 1.47 tests, searches observed the old complete map
while a replacement task was processing and the new complete map after it
succeeded.

Re-submitting an identical map still created a settings task and took about the
same time as a changed map in the isolated test. The reconciler should always
canonicalize and compare current versus desired settings before calling `PUT`.

No meme document replay is needed after a synonym update.

## Limits and current seed size

Meilisearch documents these limits per synonym key:

- at most 50 alternatives;
- at most 100 total words across all alternatives;
- excess entries may be silently ignored.

The application must enforce these limits before publishing.

As of this research, the two seed files contain:

- 465 authored groups;
- 1,233 authored terms;
- 936 active source keys after applying the three-token limit;
- 1,784 directed synonym edges after mutual expansion;
- a 63,487-byte compact JSON payload.

This payload is small enough for an initial curated catalog. On the isolated
index, applying it took about `0.69s`. Query timing must still be measured on a
production-sized index and realistic traffic.

Meilisearch 1.49 changed synonym loading to lazy lookup and reports substantial
search improvements for large synonym maps. Production is pinned to 1.47 and
requires an explicit Meilisearch data migration before an upgrade, so catalog
growth should be monitored while it remains on 1.47.

## How well this should work for MemeExpert

### Expected to work very well

- `жаба,жабы,лягушка,лягушки` inside longer Russian queries;
- `пёс,пес,собака` and similar spelling/noun variants;
- established short names such as `амогус,амогас`;
- two- or three-token template aliases;
- OCR or tags containing the alternate term in the same language.

### Expected to improve recall, but with weaker ranking

- complete catchphrase substitutions such as
  `жаба по средам -> среда мои чуваки`;
- descriptive template aliases that replace every query token;
- aliases whose OCR text contains hard punctuation between phrase words.

These should be evaluated with real query logs and a fixed, sufficiently deep
Meilisearch candidate pool. Low-ranked synonym hits can otherwise be truncated
before hybrid merging.

### Not solved by synonyms

- Russian query to English OCR, such as `жаба по средам` to
  `it is wednesday my dudes` while translation mappings remain separate;
- semantic relationships that are not true aliases;
- Russian case/number inflection beyond explicitly stored surface forms;
- misspelled or partially typed synonym keys;
- reverse lookup from keys longer than three tokens in Meilisearch 1.47.

Cross-language retrieval should use a separately reviewed translation catalog
or backend query translation/expansion. It should not be mixed into the
same-language synonym groups.

## Implemented PostgreSQL/admin model

The control plane uses separate English and Russian catalogs; a separately
reviewed translation catalog remains future work. Each language catalog has an
editable draft, one immutable published revision, and archived history.
Authored input is deliberately simple: every nonblank line is one mutual group
of comma-separated terms. Locale, revision metadata, operator reason, and
admin attribution are stored around the complete source document rather than
as per-group records.

Publishing compiles and stores an immutable JSON snapshot, compiler version,
compiler-aware SHA-256 revision hash, validation result, and statistics. The
scheduler combines published language snapshots into the complete Meilisearch
map. Its desired/actual hashes cover canonical provider JSON only, which makes
them directly comparable with Meilisearch settings.

Admin validation rejects or warns about:

- empty terms and normalized duplicates;
- a term appearing in multiple mutual groups;
- mixed-language groups;
- source keys longer than three tokens on 1.47, which become target-only
  warnings; all-long groups become inactive warnings;
- more than 50 alternatives or 100 alternative words per key;
- normalized duplicate terms within a locale and source-key collisions across
  published locales;
- destructive changes that remove a large percentage of the catalog.

The admin interface offers:

- bulk draft editing and bundled TXT seed import;
- validation summaries with line-level errors and warnings;
- publish reason and optimistic revision check;
- immutable revision history and restore-to-draft;
- sync status with desired/applied hashes, task UID, timestamps, and last safe
  error;
- a manual retry action that does not bypass PostgreSQL as source of truth.

## Reconciliation algorithm

1. Load the immutable active PostgreSQL revisions.
2. Recompile the authored sources and verify compiler version, compiled map,
   and compiler-aware revision hash before combining them deterministically.
3. Hash canonical compact provider JSON; revision hashes separately include the
   compiler version.
4. Fetch and canonicalize the current Meilisearch synonym map.
5. Record `in_sync` and stop when hashes match.
6. Submit one full `PUT` only when they differ.
7. Wait for the asynchronous task with a settings-specific timeout.
8. Fetch settings again and verify the actual hash.
9. Record the applied revision, hash, task UID, duration, and timestamp.
10. On any failure, preserve the last successful Meilisearch settings, record a
    bounded safe error, and retry later.
11. Compare the published revision generation before every durable scheduler
    state write. If a newer publish supersedes an in-flight task, preserve its
    pending desired state and immediately reconcile again.

The scheduler's PostgreSQL advisory lock makes it the single settings writer.
Run one best-effort reconciliation at scheduler startup and repeat periodically
to apply new publications and repair manual drift. Each run closes its owned
Meilisearch HTTP client after the attempt.

## Sources

- [Meilisearch synonym guide](https://www.meilisearch.com/docs/capabilities/full_text_search/relevancy/synonyms)
- [Update synonyms API](https://www.meilisearch.com/docs/reference/api/settings/update-synonyms)
- [Meilisearch ranking rules](https://www.meilisearch.com/docs/capabilities/full_text_search/relevancy/ranking_rules)
- [Meilisearch 1.45 release](https://github.com/meilisearch/meilisearch/releases/tag/v1.45.0)
- [Meilisearch 1.47 release](https://github.com/meilisearch/meilisearch/releases/tag/v1.47.0)
- [Meilisearch 1.49 release](https://github.com/meilisearch/meilisearch/releases/tag/v1.49.0)
- [Synonym normalization in 1.47.1](https://github.com/meilisearch/meilisearch/blob/v1.47.1/crates/milli/src/update/settings.rs#L700-L765)
- [Single-token derivations in 1.47.1](https://github.com/meilisearch/meilisearch/blob/v1.47.1/crates/milli/src/search/new/query_term/compute_derivations.rs#L168-L235)
- [Multiword synonym parsing in 1.47.1](https://github.com/meilisearch/meilisearch/blob/v1.47.1/crates/milli/src/search/new/query_term/parse_query.rs#L217-L282)
- [Two- and three-token query graph in 1.47.1](https://github.com/meilisearch/meilisearch/blob/v1.47.1/crates/milli/src/search/new/query_graph.rs#L95-L159)
- [Quoted-term behavior in 1.47.1](https://github.com/meilisearch/meilisearch/blob/v1.47.1/crates/milli/src/search/new/query_term/parse_query.rs#L320-L342)
- [Charabia language behavior](https://github.com/meilisearch/charabia/blob/v0.9.9/README.md#L17-L23)
