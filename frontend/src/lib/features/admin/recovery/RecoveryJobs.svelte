<script lang="ts">
  import type { AdminRecoveryJobPageRead } from '$lib/api/types';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import { ActionLink, Badge, EmptyState, Notice } from '$lib/ui';
  import {
    RECOVERY_JOB_PAGE_SIZE,
    humanizeRecoveryValue,
    recoveryCapabilityLabel,
    recoveryHref,
    type RecoveryFilters
  } from './view-model';

  let {
    page,
    filters,
    loadError
  }: {
    page: AdminRecoveryJobPageRead;
    filters: RecoveryFilters;
    loadError: string | null;
  } = $props();
</script>

<section class="mt-6 grid gap-3">
  <p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">Operational history</p>
  <h2 class="m-0 text-4xl font-black tracking-[-0.06em]">Jobs</h2>
  <p class="m-0 max-w-3xl text-muted">Follow preview preparation, active execution, completed maintenance, and failures. All administrators can inspect jobs while the original requester remains part of the audit record.</p>
</section>

{#if loadError}<Notice tone="danger" role="alert">{loadError}</Notice>{/if}

<AdminPanel title="Replay and repair jobs" class="mt-6">
  {#if page.items.length}
    <div class="overflow-x-auto">
      <table class="w-full min-w-[58rem] border-collapse text-left text-sm">
        <thead><tr><th class="px-3 py-2 font-black">Job</th><th class="px-3 py-2 font-black">Status</th><th class="px-3 py-2 font-black">Selection</th><th class="px-3 py-2 font-black">Progress</th><th class="px-3 py-2 font-black">Created</th></tr></thead>
        <tbody>
          {#each page.items as job (job.id)}
            <tr class="border-t border-line align-top">
              <td class="px-3 py-3">
                <a class="font-black underline decoration-2 underline-offset-4" href={`/admin/recovery/batches/${encodeURIComponent(job.id)}`}>{recoveryCapabilityLabel(job.action)}</a>
                <p class="mb-0 mt-1 max-w-sm text-xs text-muted">{job.reason}</p>
              </td>
              <td class="px-3 py-3"><Badge>{humanizeRecoveryValue(job.status)}</Badge></td>
              <td class="px-3 py-3">{(job.selected_root_count ?? job.total_count).toLocaleString('en-US')} roots<p class="mb-0 mt-1 text-xs text-muted">{(job.expanded_execution_count ?? job.total_count).toLocaleString('en-US')} steps</p></td>
              <td class="px-3 py-3">{job.completed_count.toLocaleString('en-US')} completed<p class="mb-0 mt-1 text-xs text-muted">{job.failed_count.toLocaleString('en-US')} failed</p></td>
              <td class="px-3 py-3">{formatAdminTimestamp(job.created_at)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if !loadError}
    <EmptyState title="No replay or repair jobs" message="Prepared and scheduled jobs will appear here." />
  {/if}

  <nav class="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4" aria-label="Recovery job history pagination">
    {#if filters.jobCursor}
      <ActionLink variant="secondary" size="compact" href={recoveryHref(filters, { section: 'jobs', jobCursor: null })}>First page</ActionLink>
    {:else}
      <span class="rounded-[14px] border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">First page</span>
    {/if}
    <span class="text-sm font-extrabold text-muted">Up to {RECOVERY_JOB_PAGE_SIZE} jobs per page</span>
    {#if page.next_cursor}
      <ActionLink size="compact" href={recoveryHref(filters, { section: 'jobs', jobCursor: page.next_cursor })}>Next page</ActionLink>
    {:else}
      <span class="rounded-[14px] border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">Next page</span>
    {/if}
  </nav>
</AdminPanel>
