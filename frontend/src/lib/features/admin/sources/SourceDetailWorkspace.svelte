<script lang="ts">
  import type { AdminSourceBackfillListRead, AdminSourceChannelRead, AdminSourcePostPageRead, AdminTelegramSessionRead } from '$lib/api/types';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import { ActionLink, Badge, Button, EmptyState, FormRow, Input, Notice, Textarea } from '$lib/ui';
  import {
    backfillStatusLabel,
    humanizePipelineValue,
    sourcePostLatestHref,
    sourcePostFilterHref,
    sourcePostPageHref,
    sourcePostIndexLabel,
    sourcePostIndexTone,
    sourceBackfillAvailability,
    syncStatusLabel
  } from './post-view-model';
  import SourceManagementPanel from './SourceManagementPanel.svelte';
  import { toSourceCardViewModel } from './view-model';

  let {
    source,
    postPage,
    backfills = { items: [] },
    backfillLoadError = null,
    postLoadError = null,
    telegramAccountsLoadError = null,
    recoveryRequestIds,
    telegramAccounts,
    paging,
    loadError,
    form
  }: {
    source: AdminSourceChannelRead | null;
    postPage: AdminSourcePostPageRead | null;
    backfills?: AdminSourceBackfillListRead;
    backfillLoadError?: string | null;
    postLoadError?: string | null;
    telegramAccountsLoadError?: string | null;
    recoveryRequestIds: { backfills: Record<string, string>; posts: Record<string, string> };
    telegramAccounts: AdminTelegramSessionRead[];
    paging: { page: number; snapshotAt: string; status?: 'failed' | 'processing' | null; hasPrevious: boolean; hasNext: boolean };
    loadError: string | null;
    form?: { message?: string; error?: boolean; recoveryJobId?: string | null } | null;
  } = $props();

  const handleLabel = $derived(source ? (source.username ? `@${source.username}` : source.platform_id) : 'Unknown source');
  const backfillActive = $derived(
    source?.backfill_status === 'queued' ||
      source?.backfill_status === 'running' ||
      source?.backfill_status === 'waiting_capacity' ||
      source?.backfill_status === 'waiting_retry'
  );
  const backfillAvailability = $derived(source ? sourceBackfillAvailability(source, telegramAccounts) : null);
  const canBackfill = $derived((backfillAvailability?.canQueue ?? false) && source?.backfill_status !== 'failed');
  const sourceModel = $derived(source ? toSourceCardViewModel(source, telegramAccounts) : null);
</script>

<p class="m-0"><a class="text-sm font-black underline decoration-2 underline-offset-4" href="/admin/sources">Back to sources</a></p>

{#if form?.message}
  <Notice tone={form.error ? 'danger' : 'success'} role={form.error ? 'alert' : 'status'}>
    {form.message}
    {#if form.recoveryJobId}
      <a class="ml-2 font-black underline decoration-2 underline-offset-4" href={`/admin/recovery/batches/${encodeURIComponent(form.recoveryJobId)}`}>Open recovery job</a>
    {/if}
  </Notice>
{/if}

{#if loadError || !source}
  <Notice tone="danger" role="alert">{loadError ?? 'Source indexing details are unavailable.'}</Notice>
{:else}
  <section class="mt-4 grid gap-3">
    <p class="m-0 text-sm font-black uppercase tracking-[0.16em] text-muted">Source indexing</p>
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 class="m-0 text-[clamp(2.2rem,7vw,4.5rem)] font-black leading-[0.9] tracking-[-0.07em]">{source.title}</h1>
        <p class="mt-2 text-muted">{handleLabel} · message-level fetch, pipeline, and search-index status</p>
      </div>
      <div class="grid justify-items-end gap-2">
        <div class="flex flex-wrap justify-end gap-2">
          <Badge>{source.platform === 'telegram' ? 'Telegram' : source.platform.toUpperCase()}</Badge>
          <Badge tone={source.history_exhausted ? 'success' : 'neutral'}>
            {source.platform !== 'telegram'
              ? 'History backfill unavailable'
              : source.history_exhausted
                ? 'Full history scanned'
                : !source.initial_catchup_completed
                  ? 'Initial fetch pending'
                  : 'Older history available'}
          </Badge>
        </div>
        {#if sourceModel?.canToggle && sourceModel.toggleLabel}
          <form method="POST" action="?/toggleSourceChannel">
            <input type="hidden" name="channel_id" value={source.id} />
            <input type="hidden" name="paused" value={source.is_paused ? 'false' : 'true'} />
            <Button type="submit" variant="secondary" size="compact">{sourceModel.toggleLabel}</Button>
          </form>
        {/if}
      </div>
    </div>
  </section>

  <AdminPanel title="Source management" class="mt-6">
    {#if telegramAccountsLoadError}
      <Notice tone="danger" role="alert">{telegramAccountsLoadError} Account-dependent controls are temporarily disabled.</Notice>
    {/if}
    <SourceManagementPanel {source} {telegramAccounts} />
  </AdminPanel>

  {#if postPage}
  <section class="mt-6 grid gap-3" aria-labelledby="source-index-summary-heading">
    <h2 id="source-index-summary-heading" class="m-0 text-2xl font-black tracking-[-0.04em]">Indexing summary</h2>
    <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8">
      {@render SummaryStat('Fetched', postPage.summary.observed_count)}
      {@render SummaryStat('Metadata captured', postPage.summary.metadata_captured_count, 'success')}
      {@render SummaryStat('Metadata missing', postPage.summary.metadata_missing_count, 'trend')}
      {@render SummaryStat('Indexed', postPage.summary.indexed_count, 'success')}
      {@render SummaryStat('Partially indexed', postPage.summary.partially_indexed_count, 'trend')}
      {@render SummaryLinkStat('Processing', postPage.summary.processing_count, sourcePostFilterHref(source.id, 'processing'), 'trend')}
      {@render SummaryLinkStat('Failed', postPage.summary.failed_count, sourcePostFilterHref(source.id, 'failed'))}
      {@render SummaryStat('Not indexable', postPage.summary.not_indexable_count)}
    </div>
  </section>

  <AdminPanel title="Fetch older messages">
    <div class="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,0.7fr)]">
      <div class="grid content-start gap-2 text-sm text-muted">
        <p class="m-0">The initial fetch covers the latest messages. Queue a bounded pass to scan older Telegram history. Oldest fetched message: {source.oldest_observed_post_id ?? 'not available yet'}.</p>
        <p class="m-0"><strong class="text-ink">Backfill status:</strong> {backfillStatusLabel(source.backfill_status)}</p>
        {#if backfillActive}
          <p class="m-0"><strong class="text-ink">Progress:</strong> {source.backfill_scanned_count.toLocaleString('en-US')} of {source.backfill_requested_count.toLocaleString('en-US')} scanned</p>
        {/if}
      </div>

      <form method="POST" action="?/backfillSourceChannel" class="grid gap-3">
        <input type="hidden" name="channel_id" value={source.id} />
        <FormRow label="Older messages to fetch" hint="Use 5,000 for one normal pass; the maximum queued pass is 50,000.">
          <Input name="message_limit" type="number" min="1" max="50000" value="5000" required disabled={!canBackfill} />
        </FormRow>
        <Button type="submit" disabled={!canBackfill}>Fetch older messages</Button>
      </form>
    </div>

    {#if source.backfill_status === 'failed'}
      <Notice tone="danger" role="alert">Backfill failed: {source.backfill_error ?? 'No error detail was recorded.'} Correct the source or account issue, then use “Resume backfill” in the history below.</Notice>
    {/if}
    {#if backfillActive}
      <Notice>Older-message backfill is {source.backfill_status}. Use “Show latest messages” below to reload the ledger with newly fetched rows; the progress count updates on each load.</Notice>
    {:else if source.history_exhausted}
      <Notice tone="success">Telegram history is fully scanned for this source.</Notice>
    {:else if !canBackfill && backfillAvailability?.reason}
      <Notice>{backfillAvailability.reason}</Notice>
    {/if}
  </AdminPanel>

  <AdminPanel title="Backfill history" class="mt-6">
    {#if backfillLoadError}<Notice tone="danger" role="alert">{backfillLoadError}</Notice>{/if}
    {#if backfills.items.length}
      <div class="overflow-x-auto">
        <table class="w-full min-w-[64rem] border-collapse text-left text-sm">
          <caption class="sr-only">Durable older-message backfill jobs and recovery controls.</caption>
          <thead>
            <tr>
              <th class="px-3 py-2 font-black">Job</th>
              <th class="px-3 py-2 font-black">Status</th>
              <th class="px-3 py-2 font-black">Progress</th>
              <th class="px-3 py-2 font-black">Cursor</th>
              <th class="px-3 py-2 font-black">Account</th>
              <th class="px-3 py-2 font-black">Last progress</th>
              <th class="px-3 py-2 font-black">Recovery</th>
            </tr>
          </thead>
          <tbody>
            {#each backfills.items as job (job.id)}
              {@const requestId = recoveryRequestIds.backfills[job.id]}
              <tr class="border-t border-line align-top">
                <td class="px-3 py-3"><strong class="break-all">{job.id}</strong><p class="mb-0 mt-1 text-xs text-muted">{job.attempt_count} attempt{job.attempt_count === 1 ? '' : 's'} · {job.quarantined_count} quarantined</p></td>
                <td class="px-3 py-3"><Badge class={job.status === 'failed' ? 'border-danger-line bg-danger-surface text-danger' : ''}>{backfillStatusLabel(job.status)}</Badge>{#if job.safe_error}<p class="mb-0 mt-2 max-w-sm text-xs text-danger">{job.safe_error}</p>{/if}</td>
                <td class="px-3 py-3">{job.scanned_count.toLocaleString('en-US')} / {job.requested_count.toLocaleString('en-US')}<p class="mb-0 mt-1 text-xs text-muted">{job.remaining_count.toLocaleString('en-US')} remaining</p></td>
                <td class="px-3 py-3">{job.cursor_post_id ?? 'Not set'}</td>
                <td class="px-3 py-3">{job.telegram_session_name ?? job.telegram_session_id ?? 'Unassigned'}</td>
                <td class="px-3 py-3">{job.last_progress_at ? formatAdminTimestamp(job.last_progress_at) : 'No progress recorded'}</td>
                <td class="px-3 py-3">
                  {#if job.capabilities.includes('resume_backfill') && requestId}
                    <details class="rounded-2xl border border-line bg-soft p-3">
                      <summary class="cursor-pointer text-sm font-black">Resume backfill</summary>
                      <form method="POST" action="?/resumeSourceBackfill" class="mt-3 grid gap-3">
                        <input type="hidden" name="channel_id" value={source.id} />
                        <input type="hidden" name="job_id" value={job.id} />
                        <input type="hidden" name="version" value={job.version} />
                        <input type="hidden" name="request_id" value={requestId} />
                        <FormRow label="Audit reason"><Textarea name="reason" rows={2} minlength={3} maxlength={500} required placeholder="What was corrected?" /></FormRow>
                        <Button type="submit" size="compact">Resume backfill</Button>
                      </form>
                    </details>
                  {:else if job.capabilities.includes('resume_backfill')}
                    <p class="m-0 max-w-xs text-xs text-danger">Reload this page before resuming; no recovery request ID is available.</p>
                  {:else}
                    <p class="m-0 max-w-xs text-xs text-muted">No safe recovery action is currently available for this job.</p>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <EmptyState title="No backfill jobs" message="A bounded older-message fetch will appear here after it is queued." />
    {/if}
  </AdminPanel>

  <section class="mt-6 grid gap-4" aria-labelledby="source-post-list-heading">
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">Message ledger</p>
        <h2 id="source-post-list-heading" class="m-0 text-3xl font-black tracking-[-0.05em]">Fetched messages</h2>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <ActionLink
          size="compact"
          href={sourcePostLatestHref(source.id, paging.status)}
          data-sveltekit-reload=""
        >Show latest messages</ActionLink>
        <Badge>{postPage.total.toLocaleString('en-US')} observed</Badge>
      </div>
    </div>
    {#if paging.status}
      <Notice>
        Showing only {paging.status} messages.
        <a class="font-black underline decoration-2 underline-offset-4" href={sourcePostLatestHref(source.id)}>Clear this filter</a>
      </Notice>
    {/if}
    <p class="m-0 text-sm text-muted">Message pages use a stable snapshot. “Show latest messages” resets to page 1 and includes newly fetched or backfilled rows.</p>

    {#if postPage.items.length}
      <div class="overflow-x-auto rounded-3xl border border-line bg-paper">
        <table class="min-w-[112rem] w-full border-collapse text-left text-sm">
          <caption class="sr-only">Telegram metadata, fetch, materialization, pipeline, and search-index state for source messages.</caption>
          <thead class="bg-soft text-chiptext">
            <tr>
              <th class="px-4 py-3 font-black">Message</th>
              <th class="px-4 py-3 font-black">Telegram context</th>
              <th class="px-4 py-3 font-black">Fetched</th>
              <th class="px-4 py-3 font-black">Materialized</th>
              <th class="px-4 py-3 font-black">Pipeline</th>
              <th class="px-4 py-3 font-black">Qdrant</th>
              <th class="px-4 py-3 font-black">Meilisearch</th>
              <th class="px-4 py-3 font-black">Result</th>
              <th class="px-4 py-3 font-black">Recovery</th>
            </tr>
          </thead>
          <tbody>
            {#each postPage.items as post (post.id)}
              {@const requestId = recoveryRequestIds.posts[post.id]}
              <tr class="border-t border-line align-top">
                <td class="px-4 py-4">
                  {#if post.telegram_url}
                    <a class="font-black underline decoration-2 underline-offset-4" href={post.telegram_url} rel="noreferrer">#{post.post_id}</a>
                  {:else}
                    <strong>#{post.post_id}</strong>
                  {/if}
                  <p class="mb-0 mt-1 text-xs text-muted">{post.published_at ? formatAdminTimestamp(post.published_at) : 'Publish time unavailable'}</p>
                </td>
                <td class="max-w-md px-4 py-4">
                  <div class="flex flex-wrap gap-2">
                    <Badge tone={post.metadata_state === 'captured' ? 'success' : 'neutral'}>
                      Metadata {post.metadata_state}
                    </Badge>
                    {#if post.is_deleted}
                      <Badge class="border-danger-line bg-danger-surface text-danger">Deleted from Telegram</Badge>
                    {/if}
                  </div>

                  {#if post.metadata_state === 'captured'}
                    {#if post.text_excerpt !== null}
                      <p class="mb-0 mt-2 whitespace-pre-wrap break-words text-sm">{post.text_excerpt}</p>
                    {:else}
                      <p class="mb-0 mt-2 text-xs text-muted">Telegram exposed no text or caption.</p>
                    {/if}

                    {#if post.media_group_id || post.reply_to_post_id}
                      <dl class="mb-0 mt-2 grid gap-1 text-xs">
                        {#if post.media_group_id}
                          <div class="flex flex-wrap gap-1"><dt class="font-black">Media group:</dt><dd class="m-0 break-all">{post.media_group_id}</dd></div>
                        {/if}
                        {#if post.reply_to_post_id}
                          <div class="flex flex-wrap gap-1"><dt class="font-black">Reply to:</dt><dd class="m-0 break-all">#{post.reply_to_post_id}</dd></div>
                        {/if}
                      </dl>
                    {/if}

                    <div class="mt-2 grid gap-1 text-xs text-muted">
                      <p class="m-0">{post.telegram_edited_at ? `Edited ${formatAdminTimestamp(post.telegram_edited_at)}` : 'No Telegram edit observed'}</p>
                      {#if post.metadata_first_observed_at}
                        <p class="m-0">Metadata first observed {formatAdminTimestamp(post.metadata_first_observed_at)}</p>
                      {/if}
                      {#if post.metadata_last_observed_at}
                        <p class="m-0">Metadata last observed {formatAdminTimestamp(post.metadata_last_observed_at)}</p>
                      {/if}
                    </div>
                  {:else}
                    <p class="mb-0 mt-2 max-w-sm text-xs text-muted">Telegram post metadata has not been captured.</p>
                  {/if}

                  {#if post.is_deleted}
                    <p class="mb-0 mt-2 text-xs text-danger">{post.deletion_observed_at ? `Deletion observed ${formatAdminTimestamp(post.deletion_observed_at)}` : 'Deletion observation time unavailable'}</p>
                  {:else}
                    <p class="mb-0 mt-2 text-xs text-muted">Not marked deleted</p>
                  {/if}
                </td>
                <td class="px-4 py-4">
                  <strong>{humanizePipelineValue(post.fetch_status)}</strong>
                  <p class="mb-0 mt-1 text-xs text-muted">Observed {formatAdminTimestamp(post.observed_at)}</p>
                  {#if post.fetch_detail}<p class="mb-0 mt-1 max-w-xs text-xs text-danger">{post.fetch_detail}</p>{/if}
                </td>
                <td class="px-4 py-4">
                  <strong>{post.meme_file_id ? 'Materialized' : 'No file'}</strong>
                  <p class="mb-0 mt-1 text-xs text-muted">{post.media_type ?? 'Unknown media'} · {humanizePipelineValue(post.ingest_status ?? post.ingest_outcome)}</p>
                  {#if post.meme_id}<a class="mt-1 inline-block text-xs font-black underline" href={`/admin/memes/${post.meme_id}`}>Open meme</a>{/if}
                </td>
                <td class="px-4 py-4">
                  <strong>{humanizePipelineValue(post.pipeline_stage)}</strong>
                  <p class="mb-0 mt-1 text-xs text-muted">{humanizePipelineValue(post.pipeline_status)}</p>
                  {#if post.pipeline_error}<p class="mb-0 mt-1 max-w-xs text-xs text-danger">{post.pipeline_error}</p>{/if}
                </td>
                <td class="px-4 py-4">{syncStatusLabel(post.qdrant_status)}</td>
                <td class="px-4 py-4">{syncStatusLabel(post.meilisearch_status)}</td>
                <td class="px-4 py-4">
                  <Badge
                    tone={sourcePostIndexTone(post.index_status)}
                    class={post.index_status === 'failed' ? 'border-danger-line bg-danger-surface text-danger' : ''}
                  >{sourcePostIndexLabel(post.index_status)}</Badge>
                </td>
                <td class="px-4 py-4">
                  {#if post.capabilities?.includes('replay_source_post') && post.version && requestId}
                    <details class="rounded-2xl border border-line bg-soft p-3">
                      <summary class="cursor-pointer text-sm font-black">Replay post</summary>
                      <form method="POST" action="?/replaySourcePost" class="mt-3 grid gap-3">
                        <input type="hidden" name="channel_id" value={source.id} />
                        <input type="hidden" name="post_id" value={post.post_id} />
                        <input type="hidden" name="version" value={post.version} />
                        <input type="hidden" name="request_id" value={requestId} />
                        <FormRow label="Audit reason"><Textarea name="reason" rows={2} minlength={3} maxlength={500} required placeholder="Why should Telegram refetch this post?" /></FormRow>
                        <Button type="submit" size="compact">Replay post</Button>
                      </form>
                    </details>
                  {:else if post.capabilities?.includes('replay_source_post') && post.version}
                    <p class="m-0 max-w-xs text-xs text-danger">Reload this page before replaying; no recovery request ID is available.</p>
                  {:else if post.index_status === 'failed' || post.index_status === 'processing'}
                    <a class="text-xs font-black underline decoration-2 underline-offset-4" href={`/admin/recovery?q=${encodeURIComponent(post.post_id)}`}>Open in recovery</a>
                    {#if post.blocked_reason}<p class="mb-0 mt-1 max-w-xs text-xs text-muted">{post.blocked_reason}</p>{/if}
                  {:else}
                    <span class="text-xs text-muted">No recovery needed</span>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <EmptyState title="No fetched messages yet" message="The latest-message catch-up or a live Telegram message will create rows here." />
    {/if}

    <nav class="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4" aria-label="Source message pagination">
      {#if paging.hasPrevious}
        <ActionLink variant="secondary" size="compact" href={sourcePostPageHref(source.id, paging.page - 1, paging.snapshotAt, paging.status)}>Previous</ActionLink>
      {:else}
        <span class="rounded-[14px] border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">Previous</span>
      {/if}
      <span class="text-sm font-extrabold text-muted">Page {paging.page}</span>
      {#if paging.hasNext}
        <ActionLink variant="secondary" size="compact" href={sourcePostPageHref(source.id, paging.page + 1, paging.snapshotAt, paging.status)}>Next</ActionLink>
      {:else}
        <span class="rounded-[14px] border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">Next</span>
      {/if}
    </nav>
  </section>
  {:else}
    <Notice class="mt-6" tone="danger" role="alert">{postLoadError ?? 'Fetched-message history is temporarily unavailable.'} Source management remains available above.</Notice>
  {/if}
{/if}

{#snippet SummaryStat(label: string, value: number, tone: 'neutral' | 'success' | 'trend' = 'neutral')}
  <div class="grid gap-1 rounded-2xl border border-line bg-paper p-4">
    <span class="text-sm font-extrabold text-muted">{label}</span>
    <Badge {tone} class="w-fit">{value.toLocaleString('en-US')}</Badge>
  </div>
{/snippet}

{#snippet SummaryLinkStat(label: string, value: number, href: string, tone: 'neutral' | 'success' | 'trend' = 'neutral')}
  <a class="grid gap-1 rounded-2xl border border-line bg-paper p-4 text-ink no-underline hover:bg-soft" {href}>
    <span class="text-sm font-extrabold text-muted">{label}</span>
    <Badge {tone} class="w-fit">{value.toLocaleString('en-US')}</Badge>
  </a>
{/snippet}
