<script lang="ts">
  import { Badge, Button, Card, Input, Textarea } from '$lib/ui';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import type {
    AdminSearchSynonymCatalogRead,
    AdminSearchSynonymLocale,
    AdminSearchSynonymValidationIssue
  } from '$lib/api/types';

  interface RequestIds {
    save: string;
    importSeed: string;
    publish: string;
    reset: string;
  }

  let {
    catalog,
    locale,
    requestIds
  }: {
    catalog: AdminSearchSynonymCatalogRead;
    locale: AdminSearchSynonymLocale;
    requestIds: RequestIds;
  } = $props();

  const localeName = $derived(locale === 'en' ? 'English' : 'Russian');
  const visibleIssues = $derived(catalog.draft.validation.issues.slice(0, 50));
  const hiddenIssueCount = $derived(Math.max(0, catalog.draft.validation.issues.length - visibleIssues.length));

  function issueLocation(issue: AdminSearchSynonymValidationIssue): string {
    if (issue.line_number === null) return '';
    return issue.term ? `Line ${issue.line_number} · ${issue.term}` : `Line ${issue.line_number}`;
  }

  function shortHash(value: string | null): string {
    return value ? value.slice(0, 12) : 'not compiled';
  }
</script>

<Card
  id={locale === 'en' ? 'english' : 'russian'}
  class="grid gap-5 scroll-mt-24"
  aria-labelledby={`${locale}-synonym-heading`}
>
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div>
      <div class="mb-2 flex flex-wrap gap-2">
        <Badge>{localeName}</Badge>
        <Badge tone={catalog.draft.validation.valid ? 'success' : 'neutral'}>
          {catalog.draft.validation.valid ? 'Ready to publish' : 'Needs fixes'}
        </Badge>
      </div>
      <h2 id={`${locale}-synonym-heading`} class="m-0 text-3xl font-black tracking-[-0.04em]">{localeName} synonym catalog</h2>
      <p class="mt-2 text-sm text-muted">
        Draft revision {catalog.draft.revision_number} · {catalog.draft.validation.group_count.toLocaleString('en-US')} groups ·
        {catalog.draft.validation.compiled_key_count.toLocaleString('en-US')} Meilisearch keys
      </p>
    </div>
    <div class="text-right text-sm text-muted">
      <p class="m-0">Draft updated {formatAdminTimestamp(catalog.draft.updated_at)}</p>
      <p class="m-0 font-mono">{shortHash(catalog.draft.compiled_hash)}</p>
      <p class="m-0">Compiler {catalog.draft.compiler_version}</p>
    </div>
  </div>

  <form method="POST" action="?/saveSearchSynonymDraft" class="grid gap-3">
    <input type="hidden" name="locale" value={locale} />
    <input type="hidden" name="request_id" value={requestIds.save} />
    <input type="hidden" name="version" value={catalog.draft.version} />
    <label class="grid gap-2 font-bold" for={`${locale}-source-text`}>
      <span>Draft source</span>
      <span class="text-sm font-normal text-muted">One mutual group per line. Separate aliases with commas.</span>
      <Textarea
        id={`${locale}-source-text`}
        name="source_text"
        value={catalog.draft.source_text}
        rows={24}
        maxlength={1_000_000}
        spellcheck="false"
        class="min-h-[32rem] font-mono text-sm leading-6"
      />
    </label>
    <div class="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
      <label class="grid gap-2 font-bold" for={`${locale}-save-reason`}>
        <span>Audit reason</span>
        <Input
          id={`${locale}-save-reason`}
          name="reason"
          minlength={3}
          maxlength={500}
          required
          placeholder="Describe what changed and why"
        />
      </label>
      <Button type="submit">Save draft</Button>
    </div>
  </form>

  <div class="grid gap-3 rounded-2xl border border-line bg-soft p-4 md:grid-cols-4">
    <div><strong class="block text-2xl">{catalog.draft.validation.group_count.toLocaleString('en-US')}</strong><span class="text-sm text-muted">groups</span></div>
    <div><strong class="block text-2xl">{catalog.draft.validation.compiled_key_count.toLocaleString('en-US')}</strong><span class="text-sm text-muted">eligible keys</span></div>
    <div><strong class="block text-2xl">{catalog.draft.validation.edge_count.toLocaleString('en-US')}</strong><span class="text-sm text-muted">directed edges</span></div>
    <div><strong class="block text-2xl">{catalog.draft.validation.payload_bytes.toLocaleString('en-US')}</strong><span class="text-sm text-muted">JSON bytes</span></div>
  </div>

  {#if visibleIssues.length}
    <div class="grid gap-2" aria-labelledby={`${locale}-validation-heading`}>
      <h3 id={`${locale}-validation-heading`} class="m-0 text-xl font-black">Validation</h3>
      {#each visibleIssues as issue}
        <div class="rounded-2xl border p-3 text-sm {issue.level === 'error' ? 'border-danger-line bg-danger-surface text-danger' : 'border-line bg-paper'}">
          <strong>{issue.level === 'error' ? 'Error' : 'Warning'} · {issue.code}</strong>
          {#if issueLocation(issue)}<span class="ml-2 text-muted">{issueLocation(issue)}</span>{/if}
          <p class="mb-0 mt-1">{issue.message}</p>
        </div>
      {/each}
      {#if hiddenIssueCount}<p class="m-0 text-sm text-muted">{hiddenIssueCount} more issues are hidden.</p>{/if}
    </div>
  {/if}

  <div class="grid gap-3 lg:grid-cols-3">
    <form method="POST" action="?/importSearchSynonymSeed" class="grid content-between gap-3 rounded-2xl border border-line p-4">
      <input type="hidden" name="locale" value={locale} />
      <input type="hidden" name="request_id" value={requestIds.importSeed} />
      <input type="hidden" name="version" value={catalog.draft.version} />
      <div>
        <h3 class="m-0 text-lg font-black">Load research seed</h3>
        <p class="text-sm text-muted">Replace this draft with the reviewed repository seed for {localeName.toLowerCase()} memes.</p>
      </div>
      <input type="hidden" name="reason" value={`Load the bundled ${localeName} meme synonym research seed.`} />
      <Button type="submit" variant="secondary">Load bundled seed</Button>
    </form>

    <form method="POST" action="?/resetSearchSynonymDraft" class="grid content-between gap-3 rounded-2xl border border-line p-4">
      <input type="hidden" name="locale" value={locale} />
      <input type="hidden" name="request_id" value={requestIds.reset} />
      <input type="hidden" name="version" value={catalog.draft.version} />
      <div>
        <h3 class="m-0 text-lg font-black">Reset draft</h3>
        <p class="text-sm text-muted">Discard draft edits and copy the currently published revision.</p>
      </div>
      <input type="hidden" name="reason" value={`Reset the ${localeName} draft to the published revision.`} />
      <Button type="submit" variant="secondary" disabled={!catalog.published}>Reset to published</Button>
    </form>

    <form method="POST" action="?/publishSearchSynonymDraft" class="grid content-between gap-3 rounded-2xl border border-line p-4">
      <input type="hidden" name="locale" value={locale} />
      <input type="hidden" name="request_id" value={requestIds.publish} />
      <input type="hidden" name="version" value={catalog.draft.version} />
      <div>
        <h3 class="m-0 text-lg font-black">Publish revision</h3>
        <p class="text-sm text-muted">Freeze this draft in PostgreSQL and request Meilisearch reconciliation.</p>
      </div>
      <label class="grid gap-2 text-sm font-bold" for={`${locale}-publish-reason`}>
        <span>Publish reason</span>
        <Input id={`${locale}-publish-reason`} name="reason" minlength={3} maxlength={500} required />
      </label>
      <label class="flex items-start gap-2 text-sm" for={`${locale}-confirm-destructive`}>
        <input id={`${locale}-confirm-destructive`} type="checkbox" name="confirm_destructive" value="true" class="mt-1 size-4" />
        <span>Allow a publish that removes more than 25% of the current keys.</span>
      </label>
      <Button type="submit" disabled={!catalog.draft.validation.valid}>Publish</Button>
    </form>
  </div>

  <details class="rounded-2xl border border-line p-4">
    <summary class="cursor-pointer font-black">Revision history</summary>
    <div class="mt-4 grid gap-3">
      {#if catalog.published}
        <div class="rounded-2xl border border-success-line bg-success-surface p-3">
          <strong>Current published revision {catalog.published.revision_number}</strong>
          <p class="m-0 text-sm text-muted">
            {formatAdminTimestamp(catalog.published.published_at ?? catalog.published.updated_at)} · {shortHash(catalog.published.compiled_hash)} · {catalog.published.compiler_version}
          </p>
          {#if catalog.published.change_note}<p class="mb-0 mt-1 text-sm">{catalog.published.change_note}</p>{/if}
        </div>
      {/if}
      {#if catalog.history.length === 0}
        <p class="m-0 text-sm text-muted">No earlier published revisions yet.</p>
      {:else}
        {#each catalog.history as revision}
          <div class="flex flex-wrap items-center justify-between gap-3 rounded-2xl bg-soft p-3">
            <div>
              <strong>Revision {revision.revision_number}</strong>
              <p class="m-0 text-sm text-muted">
                {revision.status} · {formatAdminTimestamp(revision.published_at ?? revision.updated_at)} · {shortHash(revision.compiled_hash)}
              </p>
              {#if revision.change_note}<p class="mb-0 mt-1 text-sm">{revision.change_note}</p>{/if}
            </div>
            <form method="POST" action="?/resetSearchSynonymDraft">
              <input type="hidden" name="locale" value={locale} />
              <input type="hidden" name="request_id" value={requestIds.reset} />
              <input type="hidden" name="version" value={catalog.draft.version} />
              <input type="hidden" name="revision_id" value={revision.id} />
              <input type="hidden" name="reason" value={`Restore ${localeName} synonym revision ${revision.revision_number} into the draft.`} />
              <Button type="submit" variant="secondary" size="compact">Restore to draft</Button>
            </form>
          </div>
        {/each}
      {/if}
    </div>
  </details>
</Card>
