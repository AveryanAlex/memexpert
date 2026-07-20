<script lang="ts">
  import type { AdminRecoveryCandidateRead, AdminRecoveryWorkRead } from '$lib/api/types';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import { Badge, EmptyState, Notice } from '$lib/ui';
  import RecoveryActionMenu from './RecoveryActionMenu.svelte';
  import {
    humanizeRecoveryValue,
    recoveryActionsForWork,
    recoveryBucketLabel,
    recoverySafeMessage,
    recoveryWorkKindLabel
  } from './view-model';

  let {
    work,
    candidate = null,
    requestId,
    loadError,
    form
  }: {
    work: AdminRecoveryWorkRead | null;
    candidate?: AdminRecoveryCandidateRead | null;
    requestId: string;
    loadError: string | null;
    form?: { message?: string; error?: boolean; recoveryJobId?: string | null } | null;
  } = $props();

  const actions = $derived(candidate?.actions?.length ? candidate.actions : work ? recoveryActionsForWork(work) : []);
  const activeJob = $derived(candidate?.active_job ?? work?.active_job ?? null);
  const mediaProfile = $derived(
    typeof candidate?.media_profile === 'string'
      ? candidate.media_profile
      : candidate?.media_profile?.profile ?? work?.web_video_profile ?? null
  );
  const detailEntries = $derived(work ? Object.entries(work.details) : []);
</script>

<p class="m-0"><a class="text-sm font-black underline decoration-2 underline-offset-4" href="/admin/recovery">Back to recovery</a></p>

{#if form?.message}
  <Notice tone={form.error ? 'danger' : 'success'} role={form.error ? 'alert' : 'status'}>
    {form.message}
    {#if form.recoveryJobId}
      <a class="ml-2 font-black underline decoration-2 underline-offset-4" href={`/admin/recovery/batches/${encodeURIComponent(form.recoveryJobId)}`}>Open recovery job</a>
    {/if}
  </Notice>
{/if}

{#if loadError || !work}
  <Notice tone="danger" role="alert">{loadError ?? 'Recovery work is unavailable.'}</Notice>
{:else}
  <section class="mt-4 grid gap-3">
    <p class="m-0 text-sm font-black uppercase tracking-[0.16em] text-muted">{recoveryWorkKindLabel(work.kind)}</p>
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 class="m-0 text-[clamp(2.2rem,7vw,4.5rem)] font-black leading-[0.9] tracking-[-0.07em]">{work.title}</h1>
        <p class="mt-2 text-muted">Occurred {formatAdminTimestamp(work.occurred_at)} · {humanizeRecoveryValue(work.status)}</p>
      </div>
      <Badge class={work.bucket === 'retryable' ? '' : 'border-danger-line bg-danger-surface text-danger'}>{recoveryBucketLabel(work.bucket)}</Badge>
    </div>
  </section>

  {#if work.safe_error}<Notice tone="danger" role="alert">{work.safe_error}</Notice>{/if}
  {#if work.blocked_reason}<Notice>{work.blocked_reason}</Notice>{/if}
  {#if candidate?.warnings?.length || candidate?.risks?.length}
    <Notice>
      {[...(candidate.warnings ?? []), ...(candidate.risks ?? []).map(recoverySafeMessage)].join(' ')}
    </Notice>
  {/if}

  <div class="mt-6 grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
    <div class="grid gap-4">
      <AdminPanel title="Canonical work state">
        <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          <div class="rounded-2xl border border-line bg-soft p-4"><strong>Status</strong><p class="mb-0 mt-1 text-sm text-muted">{humanizeRecoveryValue(work.status)}</p></div>
          <div class="rounded-2xl border border-line bg-soft p-4"><strong>Stage or target</strong><p class="mb-0 mt-1 text-sm text-muted">{humanizeRecoveryValue(work.target ?? work.stage)}</p></div>
          <div class="rounded-2xl border border-line bg-soft p-4"><strong>Attempts</strong><p class="mb-0 mt-1 text-sm text-muted">{work.attempt_count.toLocaleString('en-US')}</p></div>
          <div class="rounded-2xl border border-line bg-soft p-4"><strong>Normalized reason</strong><p class="mb-0 mt-1 text-sm text-muted">{humanizeRecoveryValue(work.reason ?? work.error_code)}</p></div>
          <div class="rounded-2xl border border-line bg-soft p-4"><strong>Automatic retry</strong><p class="mb-0 mt-1 text-sm text-muted">{work.next_attempt_at ? formatAdminTimestamp(work.next_attempt_at) : 'Not scheduled'}</p></div>
          <div class="rounded-2xl border border-line bg-soft p-4"><strong>Retryability</strong><p class="mb-0 mt-1 text-sm text-muted">{work.is_retryable ? 'Retryable' : 'Not retryable'}</p></div>
          <div class="rounded-2xl border border-line bg-soft p-4"><strong>Media profile</strong><p class="mb-0 mt-1 text-sm text-muted">{mediaProfile ?? 'Not applicable'}</p></div>
        </div>
      </AdminPanel>

      {#if detailEntries.length}
        <AdminPanel title="Work details">
          <dl class="m-0 grid gap-3 sm:grid-cols-2">
            {#each detailEntries as [key, value] (key)}
              <div class="rounded-2xl border border-line bg-soft p-4"><dt class="font-extrabold text-muted">{humanizeRecoveryValue(key)}</dt><dd class="m-0 mt-1 break-all">{value === null ? 'Not available' : String(value)}</dd></div>
            {/each}
          </dl>
        </AdminPanel>
      {:else}
        <EmptyState title="No additional details" message="The canonical status and identifiers above are all that is currently recorded." />
      {/if}
    </div>

    <aside class="grid content-start gap-4">
      <AdminPanel title="Identifiers">
        <dl class="m-0 grid gap-3 text-sm">
          <div><dt class="font-extrabold text-muted">Work ID</dt><dd class="m-0 break-all">{work.id}</dd></div>
          {#if work.source_label}<div><dt class="font-extrabold text-muted">Source</dt><dd class="m-0">{work.source_label}</dd></div>{/if}
          {#if work.post_id}<div><dt class="font-extrabold text-muted">Post</dt><dd class="m-0">{work.post_id}</dd></div>{/if}
          {#if work.source_channel_id}<div><dt class="font-extrabold text-muted">Source channel ID</dt><dd class="m-0 break-all">{work.source_channel_id}</dd></div>{/if}
          {#if work.meme_file_id}<div><dt class="font-extrabold text-muted">File</dt><dd class="m-0 break-all">{work.meme_file_id}</dd></div>{/if}
          {#if activeJob}<div><dt class="font-extrabold text-muted">Active job</dt><dd class="m-0"><a class="font-black underline" href={`/admin/recovery/batches/${encodeURIComponent(activeJob.id)}`}>{activeJob.id}</a></dd></div>{/if}
        </dl>
      </AdminPanel>
      {#if actions.length}
        <RecoveryActionMenu kind={work.kind} workId={work.id} version={candidate?.version ?? work.version} {requestId} {actions} stage={work.stage} />
      {:else}
        <Notice>{work.blocked_reason ?? 'No safe recovery action is currently available.'}</Notice>
      {/if}
    </aside>
  </div>
{/if}
