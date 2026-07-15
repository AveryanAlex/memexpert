<script lang="ts">
  import type {
    AdminRecoveryBatchRead,
    AdminRecoveryCapability,
    AdminRecoverySummaryRead,
    AdminRecoveryWorkPageRead
  } from '$lib/api/types';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import { Badge, Button, Card, EmptyState, FormRow, Input, Notice, Select, Textarea } from '$lib/ui';
  import RecoveryActionForm from './RecoveryActionForm.svelte';
  import {
    RECOVERY_BUCKETS,
    RECOVERY_PAGE_SIZE,
    RECOVERY_STAGES,
    RECOVERY_WORK_KINDS,
    humanizeRecoveryValue,
    recoveryBucketLabel,
    recoveryCapabilityLabel,
    recoveryHref,
    recoveryPrimaryCapability,
    recoveryWorkRequestKey,
    recoveryWorkHref,
    recoveryWorkKindLabel,
    type RecoveryFilters,
    type RecoveryWorkspaceRequestIds
  } from './view-model';

  let {
    summary,
    workPage,
    filters,
    requestIds,
    loadError,
    form
  }: {
    summary: AdminRecoverySummaryRead;
    workPage: AdminRecoveryWorkPageRead;
    filters: RecoveryFilters;
    requestIds: RecoveryWorkspaceRequestIds;
    loadError: string | null;
    form?: { message?: string; error?: boolean; batch?: AdminRecoveryBatchRead; recoveryJobId?: string | null } | null;
  } = $props();

  let batchCapability = $state<AdminRecoveryCapability>('retry_stage');

  const summaryCards = $derived([
    { bucket: 'retryable' as const, label: 'Retryable', count: summary.retryable_count, detail: 'Safe recovery is available.' },
    { bucket: 'blocked' as const, label: 'Blocked', count: summary.blocked_count, detail: 'Needs a source, account, or policy correction.' },
    { bucket: 'stuck' as const, label: 'Stuck', count: summary.stuck_count, detail: 'No useful progress within its deadline.' },
    { bucket: 'dead_lettered' as const, label: 'Dead-lettered', count: summary.dead_lettered_count, detail: 'Broker delivery ended and needs review.' }
  ]);
</script>

<section class="grid gap-3">
  <p class="m-0 text-sm font-black uppercase tracking-[0.16em] text-muted">Recovery control plane</p>
  <h1 class="m-0 text-[clamp(2.4rem,8vw,5rem)] font-black leading-[0.9] tracking-[-0.075em]">Failed and stuck work</h1>
  <p class="m-0 max-w-3xl text-muted">Inspect canonical failure state, then queue one bounded and audited recovery action. Historical failures never replay automatically.</p>
</section>

{#if form?.message}
  <Notice tone={form.error ? 'danger' : 'success'} role={form.error ? 'alert' : 'status'}>
    {form.message}
    {#if form.recoveryJobId}
      <a class="ml-2 font-black underline decoration-2 underline-offset-4" href={`/admin/recovery/batches/${encodeURIComponent(form.recoveryJobId)}`}>Open recovery job</a>
    {/if}
  </Notice>
{/if}
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
      <a class="rounded-[14px] border border-line bg-paper px-3 py-2 text-sm font-extrabold text-ink no-underline hover:bg-soft" href="/admin/recovery">Clear</a>
    </div>
  </form>
</AdminPanel>

<section class="mt-6 grid gap-4" aria-labelledby="recovery-queue-heading">
  <div class="flex flex-wrap items-end justify-between gap-3">
    <div>
      <p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">Canonical queue</p>
      <h2 id="recovery-queue-heading" class="m-0 text-3xl font-black tracking-[-0.05em]">Recovery work</h2>
    </div>
    <p class="m-0 text-sm text-muted">Snapshot {formatAdminTimestamp(workPage.snapshot_at)}</p>
  </div>

  {#if workPage.items.length}
    <div class="overflow-x-auto rounded-3xl border border-line bg-paper">
      <table class="w-full min-w-[78rem] border-collapse text-left text-sm">
        <caption class="sr-only">Failed, stuck, and dead-lettered work with declared recovery actions.</caption>
        <thead class="bg-soft text-chiptext">
          <tr>
            <th class="px-4 py-3 font-black">Select</th>
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
            {@const capability = recoveryPrimaryCapability(work.capabilities)}
            {@const canBatch = work.capabilities.includes(batchCapability)}
            <tr class="border-t border-line align-top">
              <td class="px-4 py-4">
                <input
                  type="checkbox"
                  name="item"
                  form="batch-preview-form"
                  value={JSON.stringify({ kind: work.kind, id: work.id, version: work.version })}
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
                {#if capability}
                  <RecoveryActionForm
                    kind={work.kind}
                    workId={work.id}
                    version={work.version}
                    requestId={requestIds.work[recoveryWorkRequestKey(work)]}
                    {capability}
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
      <a class="rounded-[14px] border border-line bg-paper px-3 py-2 text-sm font-extrabold text-ink no-underline hover:bg-soft" href={recoveryHref(filters, { cursor: null })}>First page</a>
    {:else}
      <span class="rounded-[14px] border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">First page</span>
    {/if}
    <span class="text-sm font-extrabold text-muted">Up to {RECOVERY_PAGE_SIZE} items per page</span>
    {#if workPage.next_cursor}
      <a class="rounded-[14px] border border-ink bg-ink px-3 py-2 text-sm font-extrabold text-paper no-underline hover:opacity-85" href={recoveryHref(filters, { cursor: workPage.next_cursor })}>Next page</a>
    {:else}
      <span class="rounded-[14px] border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">Next page</span>
    {/if}
  </nav>
</section>

<AdminPanel title="Bounded batch recovery" class="mt-6">
  <p class="mt-0 text-sm text-muted">Choose one action, select up to 1,000 compatible rows in the table, and preview them. Nothing is dispatched until the preview is explicitly scheduled.</p>
  <form id="batch-preview-form" method="POST" action="?/previewRecoveryBatch" class="grid gap-3 lg:grid-cols-2">
    <input type="hidden" name="request_id" value={requestIds.batchPreview} />
    <FormRow label="One recovery action">
      <Select name="capability" bind:value={batchCapability}>
        <option value="resume_backfill">Resume backfills</option>
        <option value="replay_source_post">Replay Telegram posts</option>
        <option value="reinspect_ingest">Re-inspect media</option>
        <option value="retry_stage">Retry pipeline stage</option>
        <option value="resync_target">Resync search target</option>
        <option value="rebuild_outbox">Rebuild outbox event</option>
        <option value="recover_dead_letter">Recover dead letter</option>
        <option value="archive_dead_letter">Archive dead letter</option>
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
    <div class="flex items-end"><Button type="submit">Preview batch</Button></div>
  </form>

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
        <form method="POST" action="?/scheduleRecoveryBatch" class="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
          <input type="hidden" name="job_id" value={form.batch.id} />
          <input type="hidden" name="version" value={form.batch.version} />
          <input type="hidden" name="reason" value={form.batch.reason} />
          <FormRow label="Type SCHEDULE" hint="Dispatch remains capacity-aware after scheduling.">
            <Input name="confirmation_phrase" autocomplete="off" required />
          </FormRow>
          <Button type="submit">Schedule batch</Button>
        </form>
      {/if}
    </div>
  {/if}
</AdminPanel>
