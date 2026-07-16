import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import SearchSynonymWorkspace from '$lib/features/admin/search-synonyms/SearchSynonymWorkspace.svelte';
import type {
  AdminSearchSynonymCatalogRead,
  AdminSearchSynonymLocale,
  AdminSearchSynonymRevisionStatus,
  AdminSearchSynonymSyncStateRead
} from '$lib/api/types';

describe('/admin/search/synonyms workspace', () => {
  it('renders locale drafts, validation, publication controls, history, and durable sync status', () => {
    const body = render(SearchSynonymWorkspace, {
      props: {
        catalogs: { en: catalog('en'), ru: catalog('ru') },
        sync: syncState(),
        requestIds: {
          en: ids('1'),
          ru: ids('2'),
          retrySync: '33333333-3333-4333-8333-333333333333'
        },
        loadedAt: '2026-07-16T12:00:00Z',
        loadError: null,
        form: null
      }
    }).body;

    expect(body).toContain('Synonym catalogs');
    expect(body).toContain('English synonym catalog');
    expect(body).toContain('Russian synonym catalog');
    expect(body).toContain('жаба,лягушка');
    expect(body).toContain('One mutual group per line');
    expect(body).toContain('action="?/saveSearchSynonymDraft"');
    expect(body).toContain('action="?/importSearchSynonymSeed"');
    expect(body).toContain('action="?/publishSearchSynonymDraft"');
    expect(body).toContain('action="?/resetSearchSynonymDraft"');
    expect(body).toContain('Allow a publish that removes more than 25%');
    expect(body).toContain('long_key_target_only');
    expect(body).toContain('Revision history');
    expect(body).toContain('Current published revision');
    expect(body).toContain('Restore to draft');
    expect(body).toContain('meili_synonyms_v1');
    expect(body).toContain('Meilisearch reconciliation');
    expect(body).toContain('action="?/retrySearchSynonymSync"');
    expect(body).toContain('memexpert-memes');
  });

  it('renders publish validation terms with guidance for resolving cross-locale collisions', () => {
    const body = render(SearchSynonymWorkspace, {
      props: {
        catalogs: { en: catalog('en'), ru: catalog('ru') },
        sync: syncState(),
        requestIds: {
          en: ids('1'),
          ru: ids('2'),
          retrySync: '33333333-3333-4333-8333-333333333333'
        },
        loadedAt: '2026-07-16T12:00:00Z',
        loadError: null,
        form: {
          message: 'The synonym draft contains publish-blocking validation errors.',
          error: true,
          locale: 'ru',
          publishValidation: {
            valid: false,
            group_count: 1,
            compiled_key_count: 2,
            edge_count: 2,
            payload_bytes: 24,
            issues: [{
              level: 'error',
              code: 'cross_locale_key_collision',
              message: 'The normalized key is already published in another locale catalog.',
              line_number: null,
              term: '123'
            }]
          }
        }
      }
    }).body;

    expect(body).toContain('Publish blocked for Russian');
    expect(body).toContain('Cross-locale key collision');
    expect(body).toContain('Key: 123');
    expect(body).toContain('Remove or rename “123” in this draft');
    expect(body).toContain('href="#russian"');
    expect(body).toContain('Review Russian draft');
  });
});

function ids(digit: string) {
  return {
    save: `${digit.repeat(8)}-${digit.repeat(4)}-4${digit.repeat(3)}-8${digit.repeat(3)}-${digit.repeat(12)}`,
    importSeed: `${digit.repeat(8)}-${digit.repeat(4)}-4${digit.repeat(3)}-9${digit.repeat(3)}-${digit.repeat(12)}`,
    publish: `${digit.repeat(8)}-${digit.repeat(4)}-4${digit.repeat(3)}-a${digit.repeat(3)}-${digit.repeat(12)}`,
    reset: `${digit.repeat(8)}-${digit.repeat(4)}-4${digit.repeat(3)}-b${digit.repeat(3)}-${digit.repeat(12)}`
  };
}

function catalog(locale: AdminSearchSynonymLocale): AdminSearchSynonymCatalogRead {
  const sourceText = locale === 'ru' ? 'жаба,лягушка' : 'wednesday frog,it is wednesday my dudes';
  const published = revision(locale, 'published', 2, sourceText);
  return {
    locale,
    draft: revision(locale, 'draft', 3, sourceText),
    published,
    history: [revision(locale, 'archived', 1, sourceText)]
  };
}

function revision(
  locale: AdminSearchSynonymLocale,
  status: AdminSearchSynonymRevisionStatus,
  revisionNumber: number,
  sourceText: string
) {
  return {
    id: `${locale}-${status}-${revisionNumber}`,
    revision_number: revisionNumber,
    status,
    source_text: sourceText,
    compiler_version: 'meili_synonyms_v1',
    compiled_hash: '0123456789abcdef',
    validation: {
      valid: true,
      group_count: 1,
      compiled_key_count: 1,
      edge_count: 1,
      payload_bytes: 64,
      issues: status === 'draft'
        ? [{ level: 'warning' as const, code: 'long_key_target_only', message: 'Long terms are target-only.', line_number: 1, term: sourceText.split(',')[1] }]
        : []
    },
    change_note: 'Initial curated catalog.',
    published_at: status === 'draft' ? null : '2026-07-16T11:00:00Z',
    created_at: '2026-07-16T10:00:00Z',
    updated_at: '2026-07-16T11:00:00Z',
    version: `${locale}-${status}-version`
  };
}

function syncState(): AdminSearchSynonymSyncStateRead {
  return {
    index_name: 'memexpert-memes',
    status: 'synced',
    desired_hash: '0123456789abcdef',
    applied_hash: '0123456789abcdef',
    actual_hash: '0123456789abcdef',
    desired_revisions: { en: 1, ru: 1 },
    last_task_uid: 42,
    requested_at: '2026-07-16T11:00:00Z',
    last_checked_at: '2026-07-16T11:01:00Z',
    last_applied_at: '2026-07-16T11:01:00Z',
    safe_error: null,
    updated_at: '2026-07-16T11:01:00Z',
    version: 'sync-version'
  };
}
