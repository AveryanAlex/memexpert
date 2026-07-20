<script lang="ts">
  import { invalidate } from '$app/navigation';
  import { onMount, untrack } from 'svelte';
  import type {
    AdminRecoveryBatchRead,
    AdminRecoveryJobItemPageRead,
    AdminRecoveryJobItemStatus,
    AdminRecoveryRetryLimit
  } from '$lib/api/types';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import { ActionLink, Badge, Button, FormRow, Input, Label, Notice, Select, Textarea } from '$lib/ui';
  import {
    RECOVERY_JOB_ITEM_PAGE_SIZE,
    RECOVERY_RETRY_LIMITS,
    humanizeRecoveryValue,
    recoveryCapabilityLabel
  } from './view-model';

  let {
    batch,
    itemsPage,
    itemFilters,
    retryFailedRequestId,
    loadError,
    itemsLoadError,
    form
  }: {
    batch: AdminRecoveryBatchRead | null;
    itemsPage: AdminRecoveryJobItemPageRead;
    itemFilters: { cursor: string | null; status: AdminRecoveryJobItemStatus | null };
    retryFailedRequestId: string;
    loadError: string | null;
    itemsLoadError: string | null;
    form?: { message?: string; error?: boolean; batch?: AdminRecoveryBatchRead; recoveryJobId?: string | null } | null;
  } = $props();

  let retainedBatch = $state<AdminRecoveryBatchRead | null>(untrack(() => batch));
  let retainedItemsPage = $state<AdminRecoveryJobItemPageRead>(untrack(() => itemsPage));
  let refreshError = $state<string | null>(null);
  let refreshing = $state(false);
  let visible = false;
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let retryLimit = $state<AdminRecoveryRetryLimit>(3);

  const currentBatch = $derived(retainedBatch);
  const visibleItems = $derived([...retainedItemsPage.items].sort(compareJobItems));
  const active = $derived(currentBatch ? isActiveStatus(currentBatch.status) : false);
  const cancellable = $derived(Boolean(
    currentBatch
      && ['preparing', 'preview', 'queued', 'running'].includes(currentBatch.status)
  ));
  const discardable = $derived(Boolean(
    currentBatch
      && (currentBatch.status === 'preparing' || currentBatch.status === 'preview')
  ));
  const retryFailedAvailable = $derived(Boolean(
    currentBatch
      && currentBatch.failed_count > 0
      && ['cancelled', 'completed', 'completed_with_failures'].includes(currentBatch.status)
  ));

  $effect(() => {
    if (batch) retainedBatch = batch;
  });

  $effect(() => {
    if (!itemsLoadError) retainedItemsPage = itemsPage;
  });

  $effect(() => {
    if (form?.batch && form.batch.id === retainedBatch?.id) retainedBatch = form.batch;
  });

  $effect(() => {
    if (loadError && retainedBatch) refreshError = loadError;
    else if (!loadError) refreshError = null;
  });

  onMount(() => {
    visible = !document.hidden;
    document.addEventListener('visibilitychange', handleVisibilityChange);
    schedulePoll();
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      clearPoll();
    };
  });

  function handleVisibilityChange(): void {
    visible = !document.hidden;
    if (!visible) {
      clearPoll();
      return;
    }
    if (active) void refreshJob();
  }

  function schedulePoll(): void {
    clearPoll();
    if (!visible || !active) return;
    pollTimer = setTimeout(() => void refreshJob(), 5_000);
  }

  function clearPoll(): void {
    if (pollTimer !== null) clearTimeout(pollTimer);
    pollTimer = null;
  }

  async function refreshJob(): Promise<void> {
    if (refreshing || !currentBatch) return;
    refreshing = true;
    try {
      await invalidate(`app:admin-recovery-job:${currentBatch.id}`);
    } catch {
      refreshError = 'Could not refresh this job. Showing the last known good state.';
    } finally {
      refreshing = false;
      schedulePoll();
    }
  }

  function countStatus(value: AdminRecoveryBatchRead, ...statuses: AdminRecoveryJobItemStatus[]): number {
    const items = value.items ?? retainedItemsPage.items;
    return items.filter((item) => statuses.includes(item.status)).length;
  }

  function jobCount(value: AdminRecoveryBatchRead, field: keyof AdminRecoveryBatchRead, ...fallbackStatuses: AdminRecoveryJobItemStatus[]): number {
    const count = value[field];
    return typeof count === 'number' ? count : countStatus(value, ...fallbackStatuses);
  }

  function exclusionGroups(value: AdminRecoveryBatchRead): Array<{ reason: string; count: number; message?: string }> {
    if (value.exclusion_groups?.length) return value.exclusion_groups;
    return Object.entries(value.exclusions_by_reason ?? value.exclusions ?? {}).map(([reason, count]) => ({ reason, count }));
  }

  function itemPageHref(cursor: string | null): string {
    if (!currentBatch) return '/admin/recovery';
    const params = new URLSearchParams();
    if (itemFilters.status) params.set('item_status', itemFilters.status);
    if (cursor) params.set('item_cursor', cursor);
    const query = params.toString();
    return `/admin/recovery/batches/${encodeURIComponent(currentBatch.id)}${query ? `?${query}` : ''}`;
  }

  function compareJobItems(a: AdminRecoveryJobItemPageRead['items'][number], b: AdminRecoveryJobItemPageRead['items'][number]): number {
    const rank = (status: AdminRecoveryJobItemStatus): number => status === 'failed' ? 0 : status === 'skipped_dependency' || status === 'skipped_stale' ? 1 : 2;
    return rank(a.status) - rank(b.status);
  }

  function isActiveStatus(status: AdminRecoveryBatchRead['status']): boolean {
    return status === 'preparing' || status === 'queued' || status === 'running' || status === 'cancelling';
  }
</script>

<p class="m-0"><a class="text-sm font-black underline decoration-2 underline-offset-4" href="/admin/recovery?view=jobs">Back to Replay &amp; Repair</a></p>
{#if form?.message}
  <Notice tone={form.error ? 'danger' : 'success'} role={form.error ? 'alert' : 'status'}>
    {form.message}
    {#if form.recoveryJobId}<a class="ml-2 font-black underline" href={`/admin/recovery/batches/${encodeURIComponent(form.recoveryJobId)}`}>Open preview job</a>{/if}
  </Notice>
{/if}

{#if (!currentBatch && loadError) || !currentBatch}
  <Notice tone="danger" role="alert">{loadError ?? 'Replay and repair job is unavailable.'}</Notice>
{:else}
  <section class="mt-4 grid gap-3">
    <p class="m-0 text-sm font-black uppercase tracking-[0.16em] text-muted">Replay and repair job</p>
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 class="m-0 text-[clamp(2.2rem,7vw,4.5rem)] font-black leading-[0.9] tracking-[-0.07em]">{recoveryCapabilityLabel(currentBatch.action)}</h1>
        <p class="mt-2 break-all text-muted">{currentBatch.id}</p>
      </div>
      <Badge>{humanizeRecoveryValue(currentBatch.status)}</Badge>
    </div>
    <div class="flex flex-wrap items-center gap-3 text-sm text-muted">
      <span>Updated {formatAdminTimestamp(currentBatch.updated_at)}</span>
      {#if active}<span aria-live="polite">Polling about every five seconds while this tab is visible.</span>{/if}
      <Button type="button" size="compact" variant="secondary" onclick={() => void refreshJob()} disabled={refreshing}>{refreshing ? 'Refreshing…' : 'Refresh now'}</Button>
    </div>
  </section>

  {#if refreshError}<Notice tone="danger" role="alert">{refreshError}</Notice>{/if}

  <div class="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
    {@render Stat('Selected roots', currentBatch.selected_root_count ?? currentBatch.total_count)}
    {@render Stat('Execution steps', currentBatch.expanded_execution_count ?? currentBatch.total_count)}
    {@render Stat('Queued', jobCount(currentBatch, 'queued_count', 'queued'))}
    {@render Stat('Waiting', currentBatch.waiting_count ?? (jobCount(currentBatch, 'waiting_capacity_count', 'waiting_capacity') + jobCount(currentBatch, 'waiting_dependency_count', 'waiting_dependency')))}
    {@render Stat('Dispatched', jobCount(currentBatch, 'dispatched_count', 'dispatched'))}
    {@render Stat('Succeeded', jobCount(currentBatch, 'succeeded_count', 'succeeded'))}
    {@render Stat('Failed', currentBatch.failed_count)}
    {@render Stat('Stale', jobCount(currentBatch, 'stale_count', 'skipped_stale'))}
    {@render Stat('Skipped', currentBatch.skipped_count ?? jobCount(currentBatch, 'skipped_dependency_count', 'skipped_dependency'))}
    {@render Stat('Cancelled', jobCount(currentBatch, 'cancelled_count', 'cancelled'))}
  </div>

  {#if currentBatch.status === 'preparing'}
    <AdminPanel title="Preparing exact preview" class="mt-6">
      <div class="grid gap-3 sm:grid-cols-3" aria-live="polite">
        {@render Stat('Canonical rows scanned', currentBatch.preparation_scanned_count ?? 0)}
        {@render Stat('Roots matched', currentBatch.preparation_matched_count ?? currentBatch.selected_root_count ?? 0)}
        {@render Stat('Excluded', currentBatch.excluded_count ?? currentBatch.preparation_excluded_count ?? 0)}
      </div>
      <p class="mb-0 text-sm text-muted">The first leased scheduler turn freezes server-owned snapshot membership; later turns resume expansion from the durable cursor after a restart. Preview expiry begins only after exact materialization finishes.</p>
    </AdminPanel>
  {/if}

  {#if exclusionGroups(currentBatch).length}
    <AdminPanel title="Snapshot exclusions" class="mt-6">
      <dl class="m-0 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {#each exclusionGroups(currentBatch) as exclusion (exclusion.reason)}
          <div class="rounded-2xl border border-line bg-soft p-4"><dt class="font-extrabold">{humanizeRecoveryValue(exclusion.reason)}</dt><dd class="m-0 mt-1 text-sm text-muted">{exclusion.count.toLocaleString('en-US')} excluded{exclusion.message ? ` · ${exclusion.message}` : ''}</dd></div>
        {/each}
      </dl>
    </AdminPanel>
  {/if}

  <div class="mt-6 grid gap-4 lg:grid-cols-2">
    <AdminPanel title="Audit">
      <dl class="m-0 grid gap-3 text-sm">
        <div><dt class="font-extrabold text-muted">Reason</dt><dd class="m-0">{currentBatch.reason}</dd></div>
        <div><dt class="font-extrabold text-muted">Scope</dt><dd class="m-0">{humanizeRecoveryValue(currentBatch.scope ?? 'stage_only')}</dd></div>
        <div><dt class="font-extrabold text-muted">Retry limit</dt><dd class="m-0">{currentBatch.retry_limit ?? 3}</dd></div>
        {#if currentBatch.requested_by_display_name || currentBatch.requested_by_admin_user_id}<div><dt class="font-extrabold text-muted">Original requester</dt><dd class="m-0">{currentBatch.requested_by_display_name ?? currentBatch.requested_by_admin_user_id}</dd></div>{/if}
        {#if currentBatch.assigned_to_display_name || currentBatch.assigned_admin_user_id || currentBatch.assigned_to_admin_user_id}<div><dt class="font-extrabold text-muted">Current operator</dt><dd class="m-0">{currentBatch.assigned_to_display_name ?? currentBatch.assigned_admin_user_id ?? currentBatch.assigned_to_admin_user_id}</dd></div>{/if}
        <div><dt class="font-extrabold text-muted">Created</dt><dd class="m-0">{formatAdminTimestamp(currentBatch.created_at)}</dd></div>
        {#if currentBatch.expires_at}<div><dt class="font-extrabold text-muted">Preview expires</dt><dd class="m-0">{formatAdminTimestamp(currentBatch.expires_at)}</dd></div>{/if}
        {#if currentBatch.scheduled_at}<div><dt class="font-extrabold text-muted">Scheduled</dt><dd class="m-0">{formatAdminTimestamp(currentBatch.scheduled_at)}</dd></div>{/if}
        {#if currentBatch.completed_at}<div><dt class="font-extrabold text-muted">Completed</dt><dd class="m-0">{formatAdminTimestamp(currentBatch.completed_at)}</dd></div>{/if}
        {#if currentBatch.cancelled_at}<div><dt class="font-extrabold text-muted">Cancelled</dt><dd class="m-0">{formatAdminTimestamp(currentBatch.cancelled_at)}</dd></div>{/if}
      </dl>
    </AdminPanel>

    <AdminPanel title="Controls">
      {#if currentBatch.status === 'preparing'}
        <Notice>The exact preview is still being materialized. Scheduling becomes available when it reaches Preview.</Notice>
      {:else if currentBatch.status === 'preview'}
        <form method="POST" action="?/scheduleRecoveryBatch" class="grid gap-3">
          <input type="hidden" name="job_id" value={currentBatch.id} />
          <input type="hidden" name="version" value={currentBatch.version} />
          <input type="hidden" name="reason" value={currentBatch.reason} />
          <p class="m-0 text-sm text-muted">The reviewed, versioned result will remain capacity-aware after scheduling.</p>
          <Button type="submit">Schedule reviewed result</Button>
        </form>
      {:else if currentBatch.status === 'cancelling'}
        <Notice>Cancellation is reconciling dispatched work. Final totals will remain accurate.</Notice>
      {:else if !cancellable}
        <Notice>This job has no pending scheduling or cancellation control.</Notice>
      {/if}

      {#if cancellable}
        <form method="POST" action="?/cancelRecoveryBatch" class="grid gap-3">
          <input type="hidden" name="job_id" value={currentBatch.id} />
          <input type="hidden" name="version" value={currentBatch.version} />
          <FormRow label={discardable ? 'Discard reason' : 'Cancellation reason'}><Textarea name="reason" rows={3} minlength={3} maxlength={500} required /></FormRow>
          <Label class="!flex items-start gap-2 rounded-2xl border border-line bg-soft p-3">
            <input type="checkbox" name="acknowledge_cancel" required class="mt-1 size-4 accent-accent" />
            <span class="text-sm">{discardable ? 'Discard this unscheduled job and stop any remaining preview preparation.' : 'Stop admitting new steps; already dispatched work will reconcile before cancellation finishes.'}</span>
          </Label>
          <Button type="submit" variant="danger">{discardable ? 'Discard job' : 'Start cancellation'}</Button>
        </form>
      {/if}

      <details class="mt-4 rounded-2xl border border-line bg-soft p-3">
        <summary class="cursor-pointer text-sm font-black">Audited handoff</summary>
        <form method="POST" action="?/handoffRecoveryBatch" class="mt-3 grid gap-3">
          <input type="hidden" name="job_id" value={currentBatch.id} />
          <input type="hidden" name="version" value={currentBatch.version} />
          <FormRow label="Assign to admin user ID"><Input name="assigned_admin_user_id" value={currentBatch.assigned_admin_user_id ?? currentBatch.assigned_to_admin_user_id ?? ''} required /></FormRow>
          <FormRow label="Handoff reason"><Textarea name="reason" rows={3} minlength={3} maxlength={500} required /></FormRow>
          <p class="m-0 text-xs text-muted">The assignee changes, but the immutable original requester remains visible in audit history.</p>
          <Button type="submit" size="compact">Record handoff</Button>
        </form>
      </details>
    </AdminPanel>
  </div>

  {#if retryFailedAvailable}
    <AdminPanel title="Retry failed items" class="mt-6">
      <p class="mt-0 text-sm text-muted">Create another versioned preview from only the failed roots. Nothing is dispatched until the new preview is reviewed and scheduled.</p>
      <form method="POST" action="?/retryFailedRecoveryBatch" class="grid gap-3 md:grid-cols-2">
        <input type="hidden" name="request_id" value={retryFailedRequestId} />
        <input type="hidden" name="job_id" value={currentBatch.id} />
        <input type="hidden" name="version" value={currentBatch.version} />
        <FormRow label="Retry limit"><Select name="retry_limit" bind:value={retryLimit}>{#each RECOVERY_RETRY_LIMITS as limit}<option value={limit}>{limit} attempt{limit === 1 ? '' : 's'}</option>{/each}</Select></FormRow>
        <FormRow label="Audit reason"><Textarea name="reason" rows={3} minlength={3} maxlength={500} required /></FormRow>
        <div class="md:col-span-2"><Button type="submit">Preview retry of failed items</Button></div>
      </form>
    </AdminPanel>
  {/if}

  <AdminPanel title="Job items" class="mt-6">
    <form method="GET" class="mb-4 flex flex-wrap items-end gap-3">
      <FormRow label="Item status">
        <Select name="item_status" value={itemFilters.status ?? ''}>
          <option value="">All statuses — failures first</option>
          {#each ['failed', 'waiting_capacity', 'waiting_dependency', 'queued', 'dispatched', 'succeeded', 'skipped_stale', 'skipped_dependency', 'cancelled'] as status}
            <option value={status}>{humanizeRecoveryValue(status)}</option>
          {/each}
        </Select>
      </FormRow>
      <Button type="submit" size="compact">Filter items</Button>
    </form>

    {#if itemsLoadError}<Notice tone="danger" role="alert">{itemsLoadError} Showing the last known good item page.</Notice>{/if}
    {#if visibleItems.length}
      <div class="overflow-x-auto">
        <table class="w-full min-w-[58rem] border-collapse text-left text-sm">
          <thead><tr><th class="px-3 py-2 font-black">Work</th><th class="px-3 py-2 font-black">Stage/action</th><th class="px-3 py-2 font-black">Status</th><th class="px-3 py-2 font-black">Attempts</th><th class="px-3 py-2 font-black">Result</th></tr></thead>
          <tbody>
            {#each visibleItems as item (item.id)}
              <tr class="border-t border-line align-top">
                <td class="px-3 py-3"><a class="font-black underline decoration-2 underline-offset-4" href={`/admin/recovery/work/${encodeURIComponent(item.work_kind)}/${encodeURIComponent(item.work_id)}`}>{humanizeRecoveryValue(item.work_kind)}</a><p class="mb-0 mt-1 break-all text-xs text-muted">{item.work_id}</p></td>
                <td class="px-3 py-3">{humanizeRecoveryValue(item.stage ?? item.action)}</td>
                <td class="px-3 py-3"><Badge>{humanizeRecoveryValue(item.status)}</Badge></td>
                <td class="px-3 py-3">{item.retryable_failures_consumed ?? item.attempt_count ?? 0} / {item.retry_limit ?? currentBatch.retry_limit ?? 3}</td>
                <td class="px-3 py-3"><strong>{humanizeRecoveryValue(item.normalized_reason)}</strong>{#if item.safe_error}<p class="mb-0 mt-1 max-w-sm text-xs text-muted">{item.safe_error}</p>{/if}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <p class="m-0 text-sm text-muted">No items match this status filter.</p>
    {/if}

    <nav class="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4" aria-label="Recovery job item pagination">
      {#if itemFilters.cursor}<ActionLink variant="secondary" size="compact" href={itemPageHref(null)}>First page</ActionLink>{:else}<span class="rounded-[14px] border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">First page</span>{/if}
      <span class="text-sm font-extrabold text-muted">Up to {RECOVERY_JOB_ITEM_PAGE_SIZE} items per page</span>
      {#if retainedItemsPage.next_cursor}<ActionLink size="compact" href={itemPageHref(retainedItemsPage.next_cursor)}>Next page</ActionLink>{:else}<span class="rounded-[14px] border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">Next page</span>{/if}
    </nav>
  </AdminPanel>
{/if}

{#snippet Stat(label: string, value: number)}
  <div class="grid gap-1 rounded-2xl border border-line bg-paper p-4">
    <span class="text-sm font-extrabold text-muted">{label}</span>
    <strong class="text-3xl font-black">{value.toLocaleString('en-US')}</strong>
  </div>
{/snippet}
