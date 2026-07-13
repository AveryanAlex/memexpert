<script lang="ts">
  import type { AdminSourceChannelRead, AdminSourcePostPageRead, AdminTelegramSessionRead } from '$lib/api/types';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import { Badge, Button, EmptyState, FormRow, Input, Notice } from '$lib/ui';
  import {
    backfillStatusLabel,
    humanizePipelineValue,
    sourcePostLatestHref,
    sourcePostPageHref,
    sourcePostIndexLabel,
    sourcePostIndexTone,
    sourceBackfillAvailability,
    syncStatusLabel
  } from './post-view-model';

  let {
    source,
    postPage,
    telegramAccounts,
    paging,
    loadError,
    form
  }: {
    source: AdminSourceChannelRead | null;
    postPage: AdminSourcePostPageRead | null;
    telegramAccounts: AdminTelegramSessionRead[];
    paging: { page: number; snapshotAt: string; hasPrevious: boolean; hasNext: boolean };
    loadError: string | null;
    form?: { message?: string; error?: boolean } | null;
  } = $props();

  const handleLabel = $derived(source ? (source.username ? `@${source.username}` : source.platform_id) : 'Unknown source');
  const backfillActive = $derived(source?.backfill_status === 'queued' || source?.backfill_status === 'running');
  const backfillAvailability = $derived(source ? sourceBackfillAvailability(source, telegramAccounts) : null);
  const canBackfill = $derived(backfillAvailability?.canQueue ?? false);
</script>

<p class="m-0"><a class="text-sm font-black underline decoration-2 underline-offset-4" href="/admin/sources">Back to sources</a></p>

{#if form?.message}
  <Notice tone={form.error ? 'danger' : 'success'} role={form.error ? 'alert' : 'status'}>{form.message}</Notice>
{/if}

{#if loadError || !source || !postPage}
  <Notice tone="danger" role="alert">{loadError ?? 'Source indexing details are unavailable.'}</Notice>
{:else}
  <section class="mt-4 grid gap-3">
    <p class="m-0 text-sm font-black uppercase tracking-[0.16em] text-muted">Source indexing</p>
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 class="m-0 text-[clamp(2.2rem,7vw,4.5rem)] font-black leading-[0.9] tracking-[-0.07em]">{source.title}</h1>
        <p class="mt-2 text-muted">{handleLabel} · message-level fetch, pipeline, and search-index status</p>
      </div>
      <div class="flex flex-wrap gap-2">
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
    </div>
  </section>

  <section class="mt-6 grid gap-3" aria-labelledby="source-index-summary-heading">
    <h2 id="source-index-summary-heading" class="m-0 text-2xl font-black tracking-[-0.04em]">Indexing summary</h2>
    <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
      {@render SummaryStat('Fetched', postPage.summary.observed_count)}
      {@render SummaryStat('Indexed', postPage.summary.indexed_count, 'success')}
      {@render SummaryStat('Partially indexed', postPage.summary.partially_indexed_count, 'trend')}
      {@render SummaryStat('Processing', postPage.summary.processing_count, 'trend')}
      {@render SummaryStat('Failed', postPage.summary.failed_count)}
      {@render SummaryStat('Not indexable', postPage.summary.not_indexable_count)}
    </div>
  </section>

  <AdminPanel title="Fetch older messages">
    <div class="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,0.7fr)]">
      <div class="grid content-start gap-2 text-sm text-muted">
        <p class="m-0">The initial fetch covers the latest messages. Queue another bounded pass to scan older Telegram history. Oldest fetched message: {source.oldest_observed_post_id ?? 'not available yet'}.</p>
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
      <Notice tone="danger" role="alert">Backfill failed: {source.backfill_error ?? 'No error detail was recorded.'} You can queue another pass after correcting the source or account issue.</Notice>
    {/if}
    {#if backfillActive}
      <Notice>Older-message backfill is {source.backfill_status}. Use “Show latest messages” below to reload the ledger with newly fetched rows; the progress count updates on each load.</Notice>
    {:else if source.history_exhausted}
      <Notice tone="success">Telegram history is fully scanned for this source.</Notice>
    {:else if !canBackfill && backfillAvailability?.reason}
      <Notice>{backfillAvailability.reason}</Notice>
    {/if}
  </AdminPanel>

  <section class="mt-6 grid gap-4" aria-labelledby="source-post-list-heading">
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">Message ledger</p>
        <h2 id="source-post-list-heading" class="m-0 text-3xl font-black tracking-[-0.05em]">Fetched messages</h2>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <a
          class="rounded-[14px] border border-ink bg-ink px-3 py-2 text-sm font-extrabold text-paper no-underline hover:opacity-85"
          href={sourcePostLatestHref(source.id)}
          data-sveltekit-reload
        >Show latest messages</a>
        <Badge>{postPage.total.toLocaleString('en-US')} observed</Badge>
      </div>
    </div>
    <p class="m-0 text-sm text-muted">Message pages use a stable snapshot. “Show latest messages” resets to page 1 and includes newly fetched or backfilled rows.</p>

    {#if postPage.items.length}
      <div class="overflow-x-auto rounded-3xl border border-line bg-paper">
        <table class="min-w-[76rem] w-full border-collapse text-left text-sm">
          <caption class="sr-only">Fetch, materialization, pipeline, and search-index state for Telegram messages.</caption>
          <thead class="bg-soft text-chiptext">
            <tr>
              <th class="px-4 py-3 font-black">Message</th>
              <th class="px-4 py-3 font-black">Fetched</th>
              <th class="px-4 py-3 font-black">Materialized</th>
              <th class="px-4 py-3 font-black">Pipeline</th>
              <th class="px-4 py-3 font-black">Qdrant</th>
              <th class="px-4 py-3 font-black">Meilisearch</th>
              <th class="px-4 py-3 font-black">Result</th>
            </tr>
          </thead>
          <tbody>
            {#each postPage.items as post (post.id)}
              <tr class="border-t border-line align-top">
                <td class="px-4 py-4">
                  {#if post.telegram_url}
                    <a class="font-black underline decoration-2 underline-offset-4" href={post.telegram_url} rel="noreferrer">#{post.post_id}</a>
                  {:else}
                    <strong>#{post.post_id}</strong>
                  {/if}
                  <p class="mb-0 mt-1 text-xs text-muted">{post.published_at ? formatAdminTimestamp(post.published_at) : 'Publish time unavailable'}</p>
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
        <a class="rounded-[14px] border border-line bg-paper px-3 py-2 text-sm font-extrabold text-ink no-underline hover:bg-soft" href={sourcePostPageHref(source.id, paging.page - 1, paging.snapshotAt)}>Previous</a>
      {:else}
        <span class="rounded-[14px] border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">Previous</span>
      {/if}
      <span class="text-sm font-extrabold text-muted">Page {paging.page}</span>
      {#if paging.hasNext}
        <a class="rounded-[14px] border border-line bg-paper px-3 py-2 text-sm font-extrabold text-ink no-underline hover:bg-soft" href={sourcePostPageHref(source.id, paging.page + 1, paging.snapshotAt)}>Next</a>
      {:else}
        <span class="rounded-[14px] border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">Next</span>
      {/if}
    </nav>
  </section>
{/if}

{#snippet SummaryStat(label: string, value: number, tone: 'neutral' | 'success' | 'trend' = 'neutral')}
  <div class="grid gap-1 rounded-2xl border border-line bg-paper p-4">
    <span class="text-sm font-extrabold text-muted">{label}</span>
    <Badge {tone} class="w-fit">{value.toLocaleString('en-US')}</Badge>
  </div>
{/snippet}
