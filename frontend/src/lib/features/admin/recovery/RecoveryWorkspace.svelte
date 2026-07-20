<script lang="ts">
  import type {
    AdminRecoveryBatchRead,
    AdminRecoveryCapability,
    AdminRecoveryJobPageRead,
    AdminRecoveryReplayScope,
    AdminRecoveryRetryLimit,
    AdminRecoverySummaryRead,
    AdminRecoveryWorkRead,
    AdminRecoveryWorkPageRead
  } from '$lib/api/types';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import { ActionLink, Badge, Button, Card, EmptyState, FormRow, Input, Label, Notice, Select, Textarea } from '$lib/ui';
  import RecoveryActionMenu from './RecoveryActionMenu.svelte';
  import RecoveryJobs from './RecoveryJobs.svelte';
  import RecoveryRegenerate from './RecoveryRegenerate.svelte';
  import {
    RECOVERY_BUCKETS,
    RECOVERY_RETRY_LIMITS,
    RECOVERY_SECTIONS,
    RECOVERY_PAGE_SIZE,
    RECOVERY_STAGES,
    RECOVERY_WORK_KINDS,
    humanizeRecoveryValue,
    recoveryActionsForWork,
    recoveryBatchAcknowledgements,
    recoveryBucketLabel,
    recoveryCapabilityLabel,
    recoveryDefaultBatchCapability,
    recoveryHref,
    recoveryWorkRequestKey,
    recoveryWorkHref,
    recoveryWorkKindLabel,
    type RecoveryFilters,
    type RecoveryWorkspaceRequestIds
  } from './view-model';

  let {
    summary,
    workPage,
    jobsPage,
    filters,
    requestIds,
    loadError,
    jobsLoadError,
    form
  }: {
    summary: AdminRecoverySummaryRead;
    workPage: AdminRecoveryWorkPageRead;
    jobsPage: AdminRecoveryJobPageRead;
    filters: RecoveryFilters;
    requestIds: RecoveryWorkspaceRequestIds;
    loadError: string | null;
    jobsLoadError: string | null;
    form?: { message?: string; error?: boolean; batch?: AdminRecoveryBatchRead; recoveryJobId?: string | null } | null;
  } = $props();

  let selectedBatchCapability = $state<AdminRecoveryCapability | null>(null);
  let selectedBatchScope = $state<AdminRecoveryReplayScope>('stage_only');
  let selectedBatchRetryLimit = $state<AdminRecoveryRetryLimit>(3);
  let selectedItemValues = $state<string[]>([]);
  let selectionPageKey = $state<string | null>(null);

  const summaryCards = $derived([
    { bucket: 'retryable' as const, label: 'Retryable', count: summary.retryable_count, detail: 'Safe recovery is available.' },
    { bucket: 'blocked' as const, label: 'Blocked', count: summary.blocked_count, detail: 'Needs a source, account, or policy correction.' },
    { bucket: 'stuck' as const, label: 'Stuck', count: summary.stuck_count, detail: 'No useful progress within its deadline.' },
    { bucket: 'dead_lettered' as const, label: 'Dead-lettered', count: summary.dead_lettered_count, detail: 'Broker delivery ended and needs review.' }
  ]);
  const batchCapability = $derived(
    selectedBatchCapability
      ?? workPage.items.flatMap(recoveryActionsForWork).find((action) => action.available)?.capability
      ?? recoveryDefaultBatchCapability(workPage.items)
  );
  const currentPageKey = $derived(currentSelectionPageKey());
  const compatibleItemValues = $derived(
    workPage.items
      .filter((work) => recoveryActionsForWork(work).some((action) => action.available && action.capability === batchCapability))
      .map(recoveryWorkSelectionValue)
  );
  const selectedItemValueSet = $derived(new Set(selectedItemValues));
  const selectedCompatibleCount = $derived(
    compatibleItemValues.filter((value) => selectedItemValueSet.has(value)).length
  );
  const selectedBatchAcknowledgements = $derived(
    recoveryBatchAcknowledgements(
      workPage.items.filter((work) => selectedItemValueSet.has(recoveryWorkSelectionValue(work))),
      batchCapability,
      selectedBatchScope
    )
  );
  const allCompatibleSelected = $derived(
    compatibleItemValues.length > 0 && selectedCompatibleCount === compatibleItemValues.length
  );
  const someCompatibleSelected = $derived(
    selectedCompatibleCount > 0 && !allCompatibleSelected
  );
  const allMatchingQueryFilters = $derived(JSON.stringify(recoveryQueryFilters()));

  $effect(() => {
    if (selectionPageKey === null) {
      selectionPageKey = currentPageKey;
      return;
    }
    if (currentPageKey === selectionPageKey) return;
    selectionPageKey = currentPageKey;
    selectedBatchCapability = null;
    selectedBatchScope = 'stage_only';
    selectedBatchRetryLimit = 3;
    selectedItemValues = [];
  });

  function changeBatchCapability(event: Event): void {
    const nextCapability = (event.currentTarget as HTMLSelectElement).value as AdminRecoveryCapability;
    if (nextCapability === batchCapability) return;
    selectedBatchCapability = nextCapability;
    selectedBatchScope = 'stage_only';
    selectedItemValues = [];
  }

  function toggleAllCompatible(): void {
    selectedItemValues = allCompatibleSelected ? [] : [...compatibleItemValues];
  }

  function recoveryWorkSelectionValue(work: Pick<AdminRecoveryWorkRead, 'kind' | 'id' | 'version'>): string {
    return JSON.stringify({ kind: work.kind, id: work.id, version: work.version });
  }

  function currentSelectionPageKey(): string {
    return JSON.stringify([
      filters.bucket,
      filters.kind,
      filters.source,
      filters.stage,
      filters.reason,
      filters.query,
      filters.cursor,
      workPage.snapshot_at,
      workPage.items.map((work) => [work.kind, work.id, work.version])
    ]);
  }

  function recoveryQueryFilters(): Record<string, string> {
    const queryFilters: Record<string, string> = {};
    if (filters.bucket) queryFilters.bucket = filters.bucket;
    if (filters.kind) queryFilters.kind = filters.kind;
    if (filters.stage) queryFilters.stage = filters.stage;
    if (filters.reason) queryFilters.reason = filters.reason;
    if (filters.query) queryFilters.query = filters.query;
    else if (filters.source) {
      if (/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(filters.source)) {
        queryFilters.source_channel_id = filters.source;
      } else queryFilters.query = filters.source;
    }
    return queryFilters;
  }
</script>

<section class="grid gap-3">
  <p class="m-0 text-sm font-black uppercase tracking-[0.16em] text-muted">Operational control plane</p>
  <h1 class="m-0 text-[clamp(2.4rem,8vw,5rem)] font-black leading-[0.9] tracking-[-0.075em]">Replay &amp; Repair</h1>
  <p class="m-0 max-w-3xl text-muted">Inspect failures, deliberately regenerate derived media, and follow every audited replay job without taking healthy catalog media offline.</p>
</section>

<nav class="mt-6 grid gap-2 sm:grid-cols-3" aria-label="Replay and Repair sections">
  {#each RECOVERY_SECTIONS as section (section.value)}
    <a
      href={recoveryHref(filters, { section: section.value, cursor: null, jobCursor: null })}
      aria-current={filters.section === section.value ? 'page' : undefined}
      class={filters.section === section.value
        ? 'rounded-2xl border border-ink bg-ink p-4 text-paper no-underline'
        : 'rounded-2xl border border-line bg-paper p-4 text-ink no-underline hover:bg-soft'}
    >
      <strong>{section.label}</strong>
      <span class="mt-1 block text-xs opacity-80">{section.description}</span>
    </a>
  {/each}
</nav>

{#if form?.message}
  <Notice tone={form.error ? 'danger' : 'success'} role={form.error ? 'alert' : 'status'}>
    {form.message}
    {#if form.recoveryJobId}
      <a class="ml-2 font-black underline decoration-2 underline-offset-4" href={`/admin/recovery/batches/${encodeURIComponent(form.recoveryJobId)}`}>Open recovery job</a>
    {/if}
  </Notice>
{/if}
{#if filters.section === 'needs_attention'}
  {#if loadError}
    <Notice tone="danger" role="alert">{loadError}</Notice>
  {/if}

<section class="mt-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label="Recovery summary">
  {#each summaryCards as card (card.bucket)}
    <Card class="m-0 p-0">
      <a
        href={recoveryHref(filters, { bucket: card.bucket, cursor: null })}
        class="grid h-full gap-3 rounded-[inherit] p-5 text-ink no-underline hover:bg-soft"
      >
        <div class="flex items-start justify-between gap-3">
          <h2 class="m-0 text-lg font-black">{card.label}</h2>
          <span class="text-4xl font-black leading-none tracking-[-0.06em]">{card.count.toLocaleString('en-US')}</span>
        </div>
        <p class="m-0 text-sm text-muted">{card.detail}</p>
      </a>
    </Card>
  {/each}
</section>

<AdminPanel title="Filter recovery work" class="mt-6">
  <form method="GET" action="/admin/recovery" class="grid gap-3 lg:grid-cols-4">
    <FormRow label="Bucket">
      <Select name="bucket" value={filters.bucket ?? ''}>
        <option value="">All buckets</option>
        {#each RECOVERY_BUCKETS as option (option.value)}<option value={option.value}>{option.label}</option>{/each}
      </Select>
    </FormRow>
    <FormRow label="Work kind">
      <Select name="kind" value={filters.kind ?? ''}>
        <option value="">All work kinds</option>
        {#each RECOVERY_WORK_KINDS as option (option.value)}<option value={option.value}>{option.label}</option>{/each}
      </Select>
    </FormRow>
    <FormRow label="Source channel ID" hint="Use Search for a channel handle.">
      <Input name="source" value={filters.source ?? ''} placeholder="Source UUID" />
    </FormRow>
    <FormRow label="Stage or target">
      <Select name="stage" value={filters.stage ?? ''}>
        <option value="">All stages</option>
        {#each RECOVERY_STAGES as option (option.value)}<option value={option.value}>{option.label}</option>{/each}
      </Select>
    </FormRow>
    <FormRow label="Normalized reason">
      <Input name="reason" value={filters.reason ?? ''} placeholder="ocr_timeout" />
    </FormRow>
    <FormRow label="Search" hint="Handle, Telegram post, file, meme, or job ID.">
      <Input name="q" value={filters.query ?? ''} placeholder="Search identifiers" />
    </FormRow>
    <div class="flex items-end gap-2">
      <Button type="submit">Apply filters</Button>
      <ActionLink variant="secondary" size="compact" href="/admin/recovery">Clear</ActionLink>
    </div>
  </form>
</AdminPanel>

<section class="mt-6 grid gap-4" aria-labelledby="recovery-queue-heading">
  <div class="flex flex-wrap items-end justify-between gap-4">
    <div>
      <p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">Canonical queue</p>
      <h2 id="recovery-queue-heading" class="m-0 text-3xl font-black tracking-[-0.05em]">Recovery work</h2>
      <p class="mb-0 mt-1 text-sm text-muted">Snapshot {formatAdminTimestamp(workPage.snapshot_at)}</p>
    </div>
    <div class="grid min-w-[18rem] gap-1">
      <label for="recovery-batch-capability" class="text-sm font-extrabold">Batch action</label>
      <Select
        id="recovery-batch-capability"
        name="capability"
        form="batch-preview-form"
        value={batchCapability}
        onchange={changeBatchCapability}
      >
        <option value="resume_backfill">Resume backfills</option>
        <option value="replay_source_post">Replay Telegram posts</option>
        <option value="reinspect_ingest">Re-inspect media</option>
        <option value="regenerate_derivatives">Regenerate derivatives</option>
        <option value="replay_stage">Replay pipeline stage</option>
        <option value="retry_stage">Retry pipeline stage</option>
        <option value="resync_target">Resync search target</option>
        <option value="rebuild_outbox">Rebuild outbox event</option>
        <option value="recover_dead_letter">Recover dead letter</option>
        <option value="archive_dead_letter">Archive dead letter</option>
      </Select>
      <p class="m-0 text-xs text-muted">{compatibleItemValues.length.toLocaleString('en-US')} compatible on this page · {selectedCompatibleCount.toLocaleString('en-US')} selected</p>
    </div>
  </div>

  {#if workPage.items.length}
    <div class="overflow-x-auto rounded-3xl border border-line bg-paper">
      <table class="w-full min-w-[78rem] border-collapse text-left text-sm">
        <caption class="sr-only">Failed, stuck, and dead-lettered work with declared recovery actions.</caption>
        <thead class="bg-soft text-chiptext">
          <tr>
            <th class="px-4 py-3 font-black">
              <label class="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={allCompatibleSelected}
                  indeterminate={someCompatibleSelected}
                  disabled={compatibleItemValues.length === 0}
                  onchange={toggleAllCompatible}
                  aria-label="Select all compatible recovery work on this page"
                  class="size-4 accent-accent"
                  data-recovery-select-all
                />
                <span>Select all</span>
              </label>
              <span class="mt-1 block text-xs font-normal text-muted">Current page</span>
            </th>
            <th class="px-4 py-3 font-black">Work</th>
            <th class="px-4 py-3 font-black">Source</th>
            <th class="px-4 py-3 font-black">Stage</th>
            <th class="px-4 py-3 font-black">Reason</th>
            <th class="px-4 py-3 font-black">Attempts</th>
            <th class="px-4 py-3 font-black">Occurred</th>
            <th class="px-4 py-3 font-black">Recovery</th>
          </tr>
        </thead>
        <tbody>
          {#each workPage.items as work (`${work.kind}:${work.id}`)}
            {@const actions = recoveryActionsForWork(work)}
            {@const canBatch = actions.some((action) => action.available && action.capability === batchCapability)}
            {@const selectionValue = recoveryWorkSelectionValue(work)}
            <tr class="border-t border-line align-top" class:bg-soft={selectedItemValueSet.has(selectionValue)}>
              <td class="px-4 py-4">
                <input
                  type="checkbox"
                  name="item"
                  form="batch-preview-form"
                  value={selectionValue}
                  bind:group={selectedItemValues}
                  disabled={!canBatch}
                  aria-label={`Select ${work.title} for ${recoveryCapabilityLabel(batchCapability)}`}
                  class="size-4 accent-accent"
                />
              </td>
              <td class="px-4 py-4">
                <a class="font-black underline decoration-2 underline-offset-4" href={recoveryWorkHref(work)}>{work.title}</a>
                <p class="mb-0 mt-1 text-xs text-muted">{recoveryWorkKindLabel(work.kind)} · {humanizeRecoveryValue(work.status)}</p>
                <Badge class={work.bucket === 'blocked' || work.bucket === 'dead_lettered' || work.bucket === 'stuck' ? 'mt-2 border-danger-line bg-danger-surface text-danger' : 'mt-2'}>{recoveryBucketLabel(work.bucket)}</Badge>
              </td>
              <td class="px-4 py-4">
                <strong>{work.source_label ?? 'No source'}</strong>
                {#if work.post_id}<p class="mb-0 mt-1 text-xs text-muted">Post {work.post_id}</p>{/if}
                {#if work.meme_file_id}<p class="mb-0 mt-1 text-xs text-muted">File {work.meme_file_id}</p>{/if}
              </td>
              <td class="px-4 py-4">{humanizeRecoveryValue(work.target ?? work.stage)}</td>
              <td class="px-4 py-4">
                <strong>{humanizeRecoveryValue(work.error_code)}</strong>
                <p class="mb-0 mt-1 max-w-sm text-xs text-muted">{work.safe_error ?? work.blocked_reason ?? 'No safe error detail was recorded.'}</p>
              </td>
              <td class="px-4 py-4">{work.attempt_count.toLocaleString('en-US')}</td>
              <td class="px-4 py-4">{formatAdminTimestamp(work.occurred_at)}</td>
              <td class="px-4 py-4">
                {#if actions.length}
                  <RecoveryActionMenu
                    kind={work.kind}
                    workId={work.id}
                    version={work.version}
                    requestId={requestIds.work[recoveryWorkRequestKey(work)]}
                    {actions}
                    stage={work.stage}
                    compact
                  />
                {:else}
                  <p class="m-0 max-w-xs text-xs text-muted">{work.blocked_reason ?? 'No safe recovery action is currently available.'}</p>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else}
    <EmptyState title="No matching recovery work" message="Change the filters or return later after new failures are recorded." />
  {/if}

  <nav class="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4" aria-label="Recovery work pagination">
    {#if filters.cursor}
      <ActionLink variant="secondary" size="compact" href={recoveryHref(filters, { cursor: null })}>First page</ActionLink>
    {:else}
      <span class="rounded-[14px] border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">First page</span>
    {/if}
    <span class="text-sm font-extrabold text-muted">Up to {RECOVERY_PAGE_SIZE} items per page</span>
    {#if workPage.next_cursor}
      <ActionLink size="compact" href={recoveryHref(filters, { cursor: workPage.next_cursor })}>Next page</ActionLink>
    {:else}
      <span class="rounded-[14px] border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">Next page</span>
    {/if}
  </nav>
</section>

<AdminPanel title="Preview selected recovery work" class="mt-6">
  <p class="mt-0 text-sm text-muted">Choose the batch action above the table, select compatible rows on the current page, and preview them. Nothing is dispatched until the preview is explicitly scheduled.</p>
  <p class="text-sm font-extrabold" aria-live="polite">{selectedCompatibleCount.toLocaleString('en-US')} of {compatibleItemValues.length.toLocaleString('en-US')} compatible rows selected for {recoveryCapabilityLabel(batchCapability)}.</p>
  <form id="batch-preview-form" method="POST" action="?/previewRecoveryBatch" class="grid gap-3 lg:grid-cols-2">
    <input type="hidden" name="request_id" value={requestIds.batchPreview} />
    <input type="hidden" name="selector_type" value="explicit" />
    <FormRow label="Replay scope">
      <Select name="scope" bind:value={selectedBatchScope}>
        <option value="stage_only">Selected stage only</option>
        {#if batchCapability === 'replay_stage'}<option value="stage_and_dependents">Stage and dependents</option>{/if}
      </Select>
    </FormRow>
    <FormRow label="Retry limit" hint="Only retryable failures consume this budget.">
      <Select name="retry_limit" bind:value={selectedBatchRetryLimit}>
        {#each RECOVERY_RETRY_LIMITS as limit}<option value={limit}>{limit} attempt{limit === 1 ? '' : 's'}</option>{/each}
      </Select>
    </FormRow>
    <FormRow label="Audit reason" hint="Applied to every item admitted to this batch.">
      <Textarea name="reason" rows={3} minlength={3} maxlength={500} required placeholder="Why is this bounded recovery safe now?" />
    </FormRow>
    {#if batchCapability === 'archive_dead_letter'}
      <FormRow label="Type ARCHIVE" hint="Archiving resolves matching dead letters without replaying them.">
        <Input name="confirmation_phrase" autocomplete="off" required />
      </FormRow>
    {/if}
    {#each selectedBatchAcknowledgements as acknowledgement (acknowledgement.key)}
      <Label class="!flex items-start gap-2 rounded-2xl border border-line bg-soft p-3">
        <input type="checkbox" name="acknowledgement" value={acknowledgement.key} required class="mt-1 size-4 accent-accent" />
        <span class="text-sm">{acknowledgement.label}</span>
      </Label>
    {/each}
    <div class="flex items-end"><Button type="submit">Preview batch</Button></div>
  </form>

  <div class="mt-5 grid gap-3 rounded-2xl border border-line bg-soft p-4">
    <div>
      <strong>Select all matching</strong>
      <p class="mb-0 mt-1 text-sm text-muted">Create one uncapped query preview from the current action and filters. Preparing returns immediately; the scheduler freezes a server-owned membership snapshot, then resumably expands and revalidates it. The page timestamp is context, not historical reconstruction.</p>
    </div>
    <form method="POST" action="?/previewRecoveryBatch" class="grid gap-3 lg:grid-cols-2" data-all-matching-preview>
      <input type="hidden" name="request_id" value={requestIds.allMatchingPreview} />
      <input type="hidden" name="action" value={batchCapability} />
      <input type="hidden" name="scope" value={selectedBatchScope} />
      <input type="hidden" name="retry_limit" value={selectedBatchRetryLimit} />
      <input type="hidden" name="selector_type" value="query" />
      <input type="hidden" name="query_filters" value={allMatchingQueryFilters} />
      <input type="hidden" name="snapshot_at" value={workPage.snapshot_at} />
      <FormRow label="Audit reason" hint="Applies to every exact match admitted after version and prerequisite checks.">
        <Textarea name="reason" rows={3} minlength={3} maxlength={500} required />
      </FormRow>
      {#if batchCapability === 'archive_dead_letter'}
        <FormRow label="Type ARCHIVE" hint="Archiving resolves every exact dead-letter match without replaying it.">
          <Input name="confirmation_phrase" autocomplete="off" required />
        </FormRow>
      {/if}
      {#if batchCapability === 'replay_stage' || batchCapability === 'regenerate_derivatives'}
        <Label class="!flex items-start gap-2 rounded-2xl border border-line bg-paper p-3">
          <input type="checkbox" name="acknowledgement" value="terminal_override" required class="mt-1 size-4 accent-accent" />
          <span class="text-sm">I acknowledge that exact materialization may include eligible terminal-failed roots and authorizes their audited override.</span>
        </Label>
      {/if}
      <div class="flex items-end"><Button type="submit">Prepare all matching</Button></div>
    </form>
  </div>

  {#if form?.batch}
    <div class="mt-4 grid gap-3 rounded-2xl border border-line bg-soft p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <strong>{recoveryCapabilityLabel(form.batch.action)}</strong>
          <p class="mb-0 mt-1 text-sm text-muted">{form.batch.total_count.toLocaleString('en-US')} selected · {form.batch.completed_count.toLocaleString('en-US')} completed</p>
        </div>
        <a class="text-sm font-black underline decoration-2 underline-offset-4" href={`/admin/recovery/batches/${encodeURIComponent(form.batch.id)}`}>Open batch</a>
      </div>
      {#if form.batch.status === 'preview'}
        <form method="POST" action="?/scheduleRecoveryBatch" class="flex flex-wrap items-center justify-between gap-3">
          <input type="hidden" name="job_id" value={form.batch.id} />
          <input type="hidden" name="version" value={form.batch.version} />
          <input type="hidden" name="reason" value={form.batch.reason} />
          <p class="m-0 text-sm text-muted">Reviewed previews dispatch gradually under the capacity budget.</p>
          <Button type="submit">Schedule batch</Button>
        </form>
      {/if}
    </div>
  {/if}
</AdminPanel>
{:else if filters.section === 'regenerate'}
  <RecoveryRegenerate
    {summary}
    snapshotAt={summary.snapshot_at ?? workPage.snapshot_at}
    outdatedRequestId={requestIds.outdatedVideoPreview}
    successfulStageRequestId={requestIds.successfulStagePreview}
    batch={form?.batch ?? null}
  />
{:else}
  <RecoveryJobs page={jobsPage} filters={filters} loadError={jobsLoadError} />
{/if}
