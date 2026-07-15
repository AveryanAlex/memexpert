<script lang="ts">
  import type { AdminRecoveryBatchRead } from '$lib/api/types';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import { Badge, Button, FormRow, Input, Notice, Textarea } from '$lib/ui';
  import { humanizeRecoveryValue, recoveryCapabilityLabel } from './view-model';

  let {
    batch,
    loadError,
    form
  }: {
    batch: AdminRecoveryBatchRead | null;
    loadError: string | null;
    form?: { message?: string; error?: boolean; batch?: AdminRecoveryBatchRead } | null;
  } = $props();

  const currentBatch = $derived(form?.batch ?? batch);
  const cancellable = $derived(currentBatch?.status === 'queued' || currentBatch?.status === 'running');

  function countStatus(value: AdminRecoveryBatchRead, status: AdminRecoveryBatchRead['items'][number]['status']): number {
    return value.items.filter((item) => item.status === status).length;
  }
</script>

<p class="m-0"><a class="text-sm font-black underline decoration-2 underline-offset-4" href="/admin/recovery">Back to recovery</a></p>
{#if form?.message}<Notice tone={form.error ? 'danger' : 'success'} role={form.error ? 'alert' : 'status'}>{form.message}</Notice>{/if}

{#if loadError || !currentBatch}
  <Notice tone="danger" role="alert">{loadError ?? 'Recovery batch is unavailable.'}</Notice>
{:else}
  <section class="mt-4 grid gap-3">
    <p class="m-0 text-sm font-black uppercase tracking-[0.16em] text-muted">Recovery batch</p>
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 class="m-0 text-[clamp(2.2rem,7vw,4.5rem)] font-black leading-[0.9] tracking-[-0.07em]">{recoveryCapabilityLabel(currentBatch.action)}</h1>
        <p class="mt-2 break-all text-muted">{currentBatch.id}</p>
      </div>
      <Badge>{humanizeRecoveryValue(currentBatch.status)}</Badge>
    </div>
  </section>

  <div class="mt-6 grid gap-4 lg:grid-cols-4">
    {@render Stat('Matched', currentBatch.total_count)}
    {@render Stat('Completed', currentBatch.completed_count)}
    {@render Stat('Waiting capacity', countStatus(currentBatch, 'waiting_capacity'))}
    {@render Stat('Succeeded', countStatus(currentBatch, 'succeeded'))}
    {@render Stat('Failed', currentBatch.failed_count)}
    {@render Stat('Queued', countStatus(currentBatch, 'queued'))}
    {@render Stat('Dispatched', countStatus(currentBatch, 'dispatched'))}
    {@render Stat('Skipped stale', countStatus(currentBatch, 'skipped_stale'))}
    {@render Stat('Cancelled', countStatus(currentBatch, 'cancelled'))}
  </div>

  <div class="mt-6 grid gap-4 lg:grid-cols-2">
    <AdminPanel title="Audit">
      <dl class="m-0 grid gap-3 text-sm">
        <div><dt class="font-extrabold text-muted">Reason</dt><dd class="m-0">{currentBatch.reason}</dd></div>
        <div><dt class="font-extrabold text-muted">Created</dt><dd class="m-0">{formatAdminTimestamp(currentBatch.created_at)}</dd></div>
        {#if currentBatch.expires_at}<div><dt class="font-extrabold text-muted">Preview expires</dt><dd class="m-0">{formatAdminTimestamp(currentBatch.expires_at)}</dd></div>{/if}
        {#if currentBatch.scheduled_at}<div><dt class="font-extrabold text-muted">Scheduled</dt><dd class="m-0">{formatAdminTimestamp(currentBatch.scheduled_at)}</dd></div>{/if}
        {#if currentBatch.completed_at}<div><dt class="font-extrabold text-muted">Completed</dt><dd class="m-0">{formatAdminTimestamp(currentBatch.completed_at)}</dd></div>{/if}
        {#if currentBatch.cancelled_at}<div><dt class="font-extrabold text-muted">Cancelled</dt><dd class="m-0">{formatAdminTimestamp(currentBatch.cancelled_at)}</dd></div>{/if}
      </dl>
    </AdminPanel>

    <AdminPanel title="Controls">
      {#if currentBatch.status === 'preview'}
        <form method="POST" action="?/scheduleRecoveryBatch" class="grid gap-3">
          <input type="hidden" name="job_id" value={currentBatch.id} />
          <input type="hidden" name="version" value={currentBatch.version} />
          <input type="hidden" name="reason" value={currentBatch.reason} />
          <FormRow label="Type SCHEDULE" hint="Items are released gradually under the capacity budget."><Input name="confirmation_phrase" autocomplete="off" required /></FormRow>
          <Button type="submit">Schedule batch</Button>
        </form>
      {:else if cancellable}
        <form method="POST" action="?/cancelRecoveryBatch" class="grid gap-3">
          <input type="hidden" name="job_id" value={currentBatch.id} />
          <input type="hidden" name="version" value={currentBatch.version} />
          <FormRow label="Cancellation reason"><Textarea name="reason" rows={3} minlength={3} maxlength={500} required /></FormRow>
          <FormRow label="Type CANCEL" hint="Only undispatched items are cancelled."><Input name="confirmation_phrase" autocomplete="off" required /></FormRow>
          <Button type="submit" variant="danger">Cancel undispatched items</Button>
        </form>
      {:else}
        <Notice>This batch has no pending manual control.</Notice>
      {/if}
    </AdminPanel>
  </div>

  {#if currentBatch.items.length}
    <AdminPanel title="Batch items" class="mt-6">
      <div class="overflow-x-auto">
        <table class="w-full min-w-[48rem] border-collapse text-left text-sm">
          <thead><tr><th class="px-3 py-2 font-black">Work</th><th class="px-3 py-2 font-black">Action</th><th class="px-3 py-2 font-black">Status</th><th class="px-3 py-2 font-black">Result</th></tr></thead>
          <tbody>
            {#each currentBatch.items as item (item.id)}
              <tr class="border-t border-line align-top">
                <td class="px-3 py-3"><a class="font-black underline decoration-2 underline-offset-4" href={`/admin/recovery/work/${encodeURIComponent(item.work_kind)}/${encodeURIComponent(item.work_id)}`}>{humanizeRecoveryValue(item.work_kind)}</a><p class="mb-0 mt-1 break-all text-xs text-muted">{item.work_id}</p></td>
                <td class="px-3 py-3">{recoveryCapabilityLabel(item.action)}</td>
                <td class="px-3 py-3">{humanizeRecoveryValue(item.status)}</td>
                <td class="px-3 py-3"><strong>{humanizeRecoveryValue(item.normalized_reason)}</strong>{#if item.safe_error}<p class="mb-0 mt-1 max-w-sm text-xs text-muted">{item.safe_error}</p>{/if}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </AdminPanel>
  {/if}
{/if}

{#snippet Stat(label: string, value: number)}
  <div class="grid gap-1 rounded-2xl border border-line bg-paper p-4">
    <span class="text-sm font-extrabold text-muted">{label}</span>
    <strong class="text-3xl font-black">{value.toLocaleString('en-US')}</strong>
  </div>
{/snippet}
