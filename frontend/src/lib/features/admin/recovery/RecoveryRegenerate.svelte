<script lang="ts">
  import type {
    AdminRecoveryBatchRead,
    AdminRecoveryReplayScope,
    AdminRecoveryRetryLimit,
    AdminRecoverySummaryRead
  } from '$lib/api/types';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { Button, FormRow, Label, Notice, Select, Textarea } from '$lib/ui';
  import { RECOVERY_RETRY_LIMITS, humanizeRecoveryValue } from './view-model';

  const CURRENT_WEB_VIDEO_PROFILE = 'web-h264-aac-1080p30-v2';
  const OUTDATED_VIDEO_FILTERS = JSON.stringify({ outdated_web_video: true });
  const SUCCESSFUL_STAGES = [
    { value: 'transcode', label: 'Transcode' },
    { value: 'ocr', label: 'OCR' },
    { value: 'embed', label: 'Embed' },
    { value: 'classify', label: 'Classify' },
    { value: 'sync_qdrant', label: 'Qdrant sync' },
    { value: 'sync_meili', label: 'Meilisearch sync' }
  ] as const;
  type SuccessfulStage = (typeof SUCCESSFUL_STAGES)[number]['value'];

  let {
    summary,
    snapshotAt,
    outdatedRequestId,
    successfulStageRequestId,
    batch
  }: {
    summary: AdminRecoverySummaryRead;
    snapshotAt: string;
    outdatedRequestId: string;
    successfulStageRequestId: string;
    batch: AdminRecoveryBatchRead | null;
  } = $props();

  let retryLimit = $state<AdminRecoveryRetryLimit>(3);
  let successfulStage = $state<SuccessfulStage>('ocr');
  let successfulScope = $state<AdminRecoveryReplayScope>('stage_only');
  let successfulRetryLimit = $state<AdminRecoveryRetryLimit>(3);
  const outdatedCount = $derived(summary.outdated_web_video_count);
  const successfulStageFilters = $derived(JSON.stringify({ successful_stage: true, stage: successfulStage }));
  const successfulStageHasDependents = $derived(!successfulStage.startsWith('sync_'));
</script>

<section class="mt-6 grid gap-3">
  <p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">Deliberate maintenance</p>
  <h2 class="m-0 text-4xl font-black tracking-[-0.06em]">Regenerate</h2>
  <p class="m-0 max-w-3xl text-muted">Replay successful or failed work deliberately. Backend-owned eligibility, versions, and prerequisites are rechecked before anything is dispatched.</p>
</section>

<AdminPanel title="Outdated web videos" class="mt-6">
  <div class="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,0.55fr)]">
    <div class="grid content-start gap-3">
      <div class="flex flex-wrap items-baseline gap-3">
        <strong class="text-5xl font-black tracking-[-0.06em]">{outdatedCount === undefined ? 'Exact scan' : outdatedCount.toLocaleString('en-US')}</strong>
        <span class="text-sm font-extrabold text-muted">matching derivatives</span>
      </div>
      <p class="m-0 text-sm text-muted">
        Matches every derivative not on <code>{CURRENT_WEB_VIDEO_PROFILE}</code>, every unverified output, and every inconsistent source/output audio state.
      </p>
      <Notice>Derivative-only regeneration creates and verifies a new web video and poster, then atomically activates them. It does not rerun OCR, embeddings, classification, or search synchronization.</Notice>
      <p class="m-0 text-xs text-muted">Select all matching is uncapped. The request returns Preparing immediately; the scheduler first freezes server-owned snapshot membership, then resumably expands and revalidates it before preview expiry starts.</p>
    </div>

    <form method="POST" action="?/previewRecoveryBatch&amp;view=regenerate" class="grid gap-3 rounded-2xl border border-line bg-soft p-4">
      <input type="hidden" name="request_id" value={outdatedRequestId} />
      <input type="hidden" name="action" value="regenerate_derivatives" />
      <input type="hidden" name="scope" value="stage_only" />
      <input type="hidden" name="selector_type" value="query" />
      <input type="hidden" name="query_filters" value={OUTDATED_VIDEO_FILTERS} />
      <input type="hidden" name="snapshot_at" value={snapshotAt} />
      <FormRow label="Retry limit" hint="Only retryable failures consume the selected budget.">
        <Select name="retry_limit" bind:value={retryLimit}>
          {#each RECOVERY_RETRY_LIMITS as limit}<option value={limit}>{limit} attempt{limit === 1 ? '' : 's'}</option>{/each}
        </Select>
      </FormRow>
      <FormRow label="Audit reason">
        <Textarea name="reason" rows={4} minlength={3} maxlength={500} required placeholder="Why should every outdated derivative be regenerated now?" />
      </FormRow>
      <Label class="!flex items-start gap-2 rounded-2xl border border-line bg-paper p-3">
        <input type="checkbox" name="acknowledgement" value="terminal_override" required class="mt-1 size-4 accent-accent" />
        <span class="text-sm">I acknowledge that exact materialization may include terminal-failed Transcode roots and authorizes their audited derivative-only override.</span>
      </Label>
      <Button type="submit">Select all matching</Button>
    </form>
  </div>
</AdminPanel>

<AdminPanel title="Replay successful stages" class="mt-6">
  <div class="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(20rem,0.65fr)]">
    <div class="grid content-start gap-3">
      <p class="m-0 text-sm text-muted">Select every currently successful canonical row for one stage. The exact, versioned preview is built asynchronously without a product-level item cap.</p>
      {#if successfulScope === 'stage_only'}
        <Notice class="border-line bg-cream">Stage-only replay leaves existing downstream data untouched and potentially stale.{successfulStage === 'transcode' ? ' Moving-media Transcode replaces only the verified derivative pair.' : ''}</Notice>
      {:else}
        <Notice>Cascading replay follows the dependency chain. Provider output and semantic merges may change; Qdrant and Meilisearch may run concurrently after Classify.</Notice>
      {/if}
      {#if !successfulStageHasDependents}
        <p class="m-0 text-xs text-muted">This search-sync stage has no downstream dependents, so both scopes execute only the selected target.</p>
      {/if}
    </div>

    <form method="POST" action="?/previewRecoveryBatch&amp;view=regenerate" class="grid gap-3 rounded-2xl border border-line bg-soft p-4" data-successful-stage-preview>
      <input type="hidden" name="request_id" value={successfulStageRequestId} />
      <input type="hidden" name="action" value="replay_stage" />
      <input type="hidden" name="selector_type" value="query" />
      <input type="hidden" name="query_filters" value={successfulStageFilters} />
      <input type="hidden" name="snapshot_at" value={snapshotAt} />
      <FormRow label="Successful stage">
        <Select name="successful_stage" bind:value={successfulStage}>
          {#each SUCCESSFUL_STAGES as stage (stage.value)}<option value={stage.value}>{stage.label}</option>{/each}
        </Select>
      </FormRow>
      <FormRow label="Replay scope">
        <Select name="scope" bind:value={successfulScope}>
          <option value="stage_only">Selected stage only</option>
          <option value="stage_and_dependents">Stage and dependents</option>
        </Select>
      </FormRow>
      <FormRow label="Retry limit" hint="Only retryable failures consume the selected budget.">
        <Select name="retry_limit" bind:value={successfulRetryLimit}>
          {#each RECOVERY_RETRY_LIMITS as limit}<option value={limit}>{limit} attempt{limit === 1 ? '' : 's'}</option>{/each}
        </Select>
      </FormRow>
      <FormRow label="Audit reason">
        <Textarea name="reason" rows={4} minlength={3} maxlength={500} required placeholder="Why should every successful row in this stage be replayed?" />
      </FormRow>
      {#if successfulScope === 'stage_and_dependents' && successfulStageHasDependents}
        <Label class="!flex items-start gap-2 rounded-2xl border border-line bg-paper p-3">
          <input type="checkbox" name="acknowledgement" value="terminal_override" required class="mt-1 size-4 accent-accent" />
          <span class="text-sm">I acknowledge that the exact cascade may include terminal-failed descendants and authorizes their audited replay override.</span>
        </Label>
      {/if}
      <Button type="submit">Select all matching successful stages</Button>
    </form>
  </div>
</AdminPanel>

{#if batch}
  <AdminPanel title="Latest preview job" class="mt-6">
    <div class="grid gap-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <strong class="text-xl">{humanizeRecoveryValue(batch.status)}</strong>
          <p class="mb-0 mt-1 text-sm text-muted">{batch.selected_root_count ?? batch.total_count} selected roots · {batch.expanded_execution_count ?? batch.total_count} execution steps</p>
        </div>
        <a class="text-sm font-black underline decoration-2 underline-offset-4" href={`/admin/recovery/batches/${encodeURIComponent(batch.id)}`}>Open job</a>
      </div>

      {#if batch.status === 'preparing'}
        <div class="grid gap-2 rounded-2xl border border-line bg-soft p-4" aria-live="polite">
          <strong>Preparing exact preview</strong>
          <p class="m-0 text-sm text-muted">{(batch.preparation_scanned_count ?? 0).toLocaleString('en-US')} scanned · {(batch.preparation_matched_count ?? batch.selected_root_count ?? 0).toLocaleString('en-US')} matched · {(batch.preparation_excluded_count ?? batch.excluded_count ?? 0).toLocaleString('en-US')} excluded</p>
        </div>
      {:else if batch.status === 'preview'}
        <form method="POST" action="?/scheduleRecoveryBatch&amp;view=regenerate" class="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-line bg-soft p-4">
          <input type="hidden" name="job_id" value={batch.id} />
          <input type="hidden" name="version" value={batch.version} />
          <input type="hidden" name="reason" value={batch.reason} />
          <p class="m-0 text-sm text-muted">The exact materialized result is ready for one-click scheduling.</p>
          <Button type="submit">Schedule reviewed result</Button>
        </form>
      {/if}
    </div>
  </AdminPanel>
{/if}
