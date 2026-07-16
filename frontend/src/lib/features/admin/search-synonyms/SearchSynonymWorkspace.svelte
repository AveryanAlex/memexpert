<script lang="ts">
  import { ActionLink, Badge, Button, Card, Input, Notice, PageHeader } from '$lib/ui';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import type {
    AdminSearchSynonymCatalogRead,
    AdminSearchSynonymLocale,
    AdminSearchSynonymSyncStateRead,
    AdminSearchSynonymValidationIssue,
    AdminSearchSynonymValidationRead
  } from '$lib/api/types';
  import SynonymCatalogEditor from './SynonymCatalogEditor.svelte';

  interface LocaleRequestIds {
    save: string;
    importSeed: string;
    publish: string;
    reset: string;
  }

  interface RequestIds {
    en: LocaleRequestIds;
    ru: LocaleRequestIds;
    retrySync: string;
  }

  interface SearchSynonymActionForm {
    message?: string;
    error?: boolean;
    locale?: AdminSearchSynonymLocale;
    publishValidation?: AdminSearchSynonymValidationRead;
  }

  let {
    catalogs,
    sync,
    requestIds,
    loadedAt,
    loadError,
    form
  }: {
    catalogs: { en: AdminSearchSynonymCatalogRead; ru: AdminSearchSynonymCatalogRead } | null;
    sync: AdminSearchSynonymSyncStateRead | null;
    requestIds: RequestIds;
    loadedAt: string;
    loadError: string | null;
    form: SearchSynonymActionForm | null;
  } = $props();

  const canRetrySync = $derived(
    sync !== null && sync.desired_hash !== null && Object.keys(sync.desired_revisions).length > 0
  );
  const publishValidationIssues = $derived(
    form?.publishValidation?.issues.filter((issue) => issue.level === 'error').slice(0, 50) ?? []
  );
  const hiddenPublishIssueCount = $derived(
    Math.max(
      0,
      (form?.publishValidation?.issues.filter((issue) => issue.level === 'error').length ?? 0)
        - publishValidationIssues.length
    )
  );

  function shortHash(value: string | null): string {
    return value ? value.slice(0, 12) : 'none';
  }

  function optionalTimestamp(value: string | null): string {
    return value ? formatAdminTimestamp(value) : 'never';
  }

  function syncTone(status: AdminSearchSynonymSyncStateRead['status']): 'neutral' | 'success' | 'trend' {
    return status === 'synced' ? 'success' : status === 'pending' || status === 'syncing' ? 'trend' : 'neutral';
  }

  function desiredRevisionSummary(revisions: Partial<Record<AdminSearchSynonymLocale, number>>): string {
    const labels = (['en', 'ru'] as const).flatMap((locale) => {
      const revision = revisions[locale];
      return revision === undefined ? [] : [`${locale.toUpperCase()} ${revision}`];
    });
    return labels.length ? labels.join(' · ') : 'none';
  }

  function localeName(locale: AdminSearchSynonymLocale): string {
    return locale === 'en' ? 'English' : 'Russian';
  }

  function issueLocation(issue: AdminSearchSynonymValidationIssue): string | null {
    if (issue.term) return issue.line_number === null ? `Key: ${issue.term}` : `Line ${issue.line_number} · ${issue.term}`;
    return issue.line_number === null ? null : `Line ${issue.line_number}`;
  }
</script>

<PageHeader
  eyebrow="Search operations"
  title="Synonym catalogs"
  description="Curate English and Russian aliases in PostgreSQL, publish immutable revisions, and monitor their reconciliation into Meilisearch."
  badge={`Loaded ${formatAdminTimestamp(loadedAt)}`}
>
  <ActionLink href="#english" variant="secondary">English</ActionLink>
  <ActionLink href="#russian" variant="secondary">Russian</ActionLink>
  <ActionLink href="/admin" variant="secondary">Admin overview</ActionLink>
</PageHeader>

{#if form?.publishValidation && form.locale}
  <Card
    role="alert"
    aria-labelledby="publish-validation-heading"
    class="mb-5 grid gap-3 border-danger-line bg-danger-surface text-danger"
  >
    <div>
      <h2 id="publish-validation-heading" class="m-0 text-xl font-black">
        Publish blocked for {localeName(form.locale)}
      </h2>
      <p class="mb-0 mt-1">{form.message ?? 'Resolve the blocking synonym issues and publish again.'}</p>
    </div>
    <div class="grid gap-2">
      {#each publishValidationIssues as issue}
        <div class="rounded-2xl border border-danger-line bg-paper p-3 text-ink">
          <div class="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <strong>{issue.code === 'cross_locale_key_collision' ? 'Cross-locale key collision' : issue.code}</strong>
            {#if issueLocation(issue)}<span class="font-mono text-sm text-muted">{issueLocation(issue)}</span>{/if}
          </div>
          <p class="mb-0 mt-1 text-sm">{issue.message}</p>
          {#if issue.code === 'cross_locale_key_collision' && issue.term}
            <p class="mb-0 mt-1 text-sm font-semibold">
              Remove or rename “{issue.term}” in this draft, or first remove it from the other locale's published catalog.
            </p>
          {/if}
        </div>
      {/each}
      {#if hiddenPublishIssueCount}
        <p class="m-0 text-sm">{hiddenPublishIssueCount} more blocking issues are hidden.</p>
      {/if}
    </div>
    <div>
      <ActionLink href={form.locale === 'en' ? '#english' : '#russian'} variant="secondary" size="compact">
        Review {localeName(form.locale)} draft
      </ActionLink>
    </div>
  </Card>
{:else if form?.message}
  <Notice tone={form.error ? 'danger' : 'success'} role={form.error ? 'alert' : 'status'}>{form.message}</Notice>
{/if}

{#if loadError}
  <Notice tone="danger" role="alert">{loadError}</Notice>
{/if}

{#if sync}
  <Card class="mb-5 grid gap-4" aria-labelledby="synonym-sync-heading">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <div class="mb-2 flex flex-wrap gap-2">
          <Badge tone={syncTone(sync.status)}>{sync.status.replace('_', ' ')}</Badge>
          <Badge>{sync.index_name}</Badge>
        </div>
        <h2 id="synonym-sync-heading" class="m-0 text-2xl font-black">Meilisearch reconciliation</h2>
        <dl class="mt-2 grid gap-x-4 gap-y-1 text-sm text-muted sm:grid-cols-2">
          <div><dt class="inline font-bold">Desired hash</dt><dd class="ml-2 inline font-mono">{shortHash(sync.desired_hash)}</dd></div>
          <div><dt class="inline font-bold">Applied hash</dt><dd class="ml-2 inline font-mono">{shortHash(sync.applied_hash)}</dd></div>
          <div><dt class="inline font-bold">Actual hash</dt><dd class="ml-2 inline font-mono">{shortHash(sync.actual_hash)}</dd></div>
          <div><dt class="inline font-bold">Desired revisions</dt><dd class="ml-2 inline">{desiredRevisionSummary(sync.desired_revisions)}</dd></div>
        </dl>
      </div>
      <div class="text-sm text-muted">
        <p class="m-0">Last checked {optionalTimestamp(sync.last_checked_at)}</p>
        <p class="m-0">Last applied {optionalTimestamp(sync.last_applied_at)}</p>
        {#if sync.last_task_uid !== null}<p class="m-0">Task {sync.last_task_uid}</p>{/if}
      </div>
    </div>
    {#if sync.safe_error}<Notice tone="danger" role="alert">{sync.safe_error}</Notice>{/if}
    <form method="POST" action="?/retrySearchSynonymSync" class="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
      <input type="hidden" name="request_id" value={requestIds.retrySync} />
      <input type="hidden" name="version" value={sync.version} />
      <label class="grid gap-2 font-bold" for="sync-retry-reason">
        <span>Retry reason</span>
        <Input id="sync-retry-reason" name="reason" minlength={3} maxlength={500} required disabled={!canRetrySync} placeholder="Why should reconciliation run again?" />
        {#if !canRetrySync}<span class="text-sm font-normal text-muted">Publish at least one synonym revision before requesting reconciliation.</span>{/if}
      </label>
      <Button type="submit" variant="secondary" disabled={!canRetrySync}>Request retry</Button>
    </form>
  </Card>
{/if}

{#if catalogs}
  <div class="grid gap-5">
    <SynonymCatalogEditor catalog={catalogs.en} locale="en" requestIds={requestIds.en} />
    <SynonymCatalogEditor catalog={catalogs.ru} locale="ru" requestIds={requestIds.ru} />
  </div>
{/if}
