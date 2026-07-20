<script lang="ts">
  import { navigating } from '$app/state';
  import type {
    PublicMemeAnalyticsRead,
    PublicMemeSourcePageRead,
    PublicMemeSourceRateRead,
    PublicMemeSourceSummaryRead,
    PublicMemeSourceTotalsRead
  } from '$lib/api/types';
  import { Badge, Card, Notice, PillLink } from '$lib/ui';
  import MemeActivityCharts from './MemeActivityCharts.svelte';
  import {
    MEME_ANALYTICS_WINDOWS,
    MEME_ANALYTICS_ANCHOR,
    MEME_SOURCES_ANCHOR,
    MEME_SOURCE_SORTS,
    memeInsightsHref,
    type MemeInsightsParams
  } from './meme-insights-params';

  interface Props {
    sourcePage: PublicMemeSourcePageRead | null;
    sourceError: string | null;
    analytics: PublicMemeAnalyticsRead | null;
    analyticsError: string | null;
    insightsParams: MemeInsightsParams;
    pathname: string;
    search: string;
  }

  let {
    sourcePage,
    sourceError,
    analytics,
    analyticsError,
    insightsParams,
    pathname,
    search
  }: Props = $props();

  const countFormatter = new Intl.NumberFormat('en');
  const decimalFormatter = new Intl.NumberFormat('en', { maximumFractionDigits: 1 });
  const sourceSummary: PublicMemeSourceSummaryRead | null = $derived(
    sourcePage?.summary ?? analytics?.source_performance ?? null
  );
  const isUpdating = $derived(navigating.to?.url.pathname === pathname);
  const pageStart = $derived(sourcePage && sourcePage.items.length > 0 ? sourcePage.offset + 1 : 0);
  const pageEnd = $derived(
    sourcePage && sourcePage.items.length > 0
      ? Math.min(sourcePage.offset + sourcePage.items.length, sourcePage.total)
      : 0
  );
  const currentSearchParams = $derived(new URLSearchParams(search));
  let sourcesOpen = $state(false);
  let analyticsOpen = $state(false);

  $effect(() => {
    if (hasSourcePresentationState(search)) sourcesOpen = true;
    if (new URLSearchParams(search).has('activity_window')) {
      sourcesOpen = true;
      analyticsOpen = true;
    }
  });

  function href(changes: Parameters<typeof memeInsightsHref>[2]): string {
    return memeInsightsHref(pathname, currentSearchParams, changes);
  }

  function hasSourcePresentationState(value: string): boolean {
    const params = new URLSearchParams(value);
    return ['source_sort', 'source_offset', 'source_snapshot', 'activity_window'].some((key) => params.has(key));
  }

  function formatCount(value: number | null | undefined): string {
    return value === null || value === undefined ? 'Unknown' : countFormatter.format(value);
  }

  function formatCompactCount(value: number | null | undefined): string {
    if (value === null || value === undefined) return 'unknown';
    return new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
  }

  function formatRate(value: number | null | undefined): string {
    if (value === null || value === undefined) return 'Unknown';
    return new Intl.NumberFormat('en', { style: 'percent', maximumFractionDigits: 2 }).format(value);
  }

  function formatPerThousand(value: number | null | undefined): string {
    return value === null || value === undefined ? 'Unknown' : decimalFormatter.format(value);
  }

  function formatSignedCount(value: number | null): string {
    if (value === null) return 'Unknown';
    return `${value > 0 ? '+' : ''}${countFormatter.format(value)}`;
  }

  function formatDate(value: string | null | undefined, includeTime = false): string {
    if (!value) return 'Unknown';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'Unknown';
    return new Intl.DateTimeFormat('en', {
      dateStyle: 'medium',
      ...(includeTime ? { timeStyle: 'short' as const } : {}),
      timeZone: 'UTC'
    }).format(date);
  }

  function coverageLabel(rate: PublicMemeSourceRateRead): string {
    return `${formatCount(rate.eligible_posts)} of ${formatCount(rate.total_posts)} posts eligible`;
  }

  function knownTotalsLabel(totals: PublicMemeSourceTotalsRead): string {
    return [
      `${formatCompactCount(totals.views)} views`,
      `${formatCompactCount(totals.reactions)} reactions`,
      `${formatCompactCount(totals.reposts)} reposts`
    ].join(' · ');
  }
</script>

<details id={MEME_SOURCES_ANCHOR} bind:open={sourcesOpen} class="mt-8 rounded-xl border border-line bg-paper" data-meme-insights>
  <summary class="cursor-pointer px-4 py-4 text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent sm:px-5">
    <span class="flex flex-wrap items-center justify-between gap-3">
      <span>
        <span class="block text-lg font-black">Sources &amp; activity</span>
        <span class="mt-0.5 block text-sm font-normal text-muted">
          {#if sourceSummary}
            {sourceSummary.total_posts} Telegram post{sourceSummary.total_posts === 1 ? '' : 's'} across {sourceSummary.distinct_channels} channel{sourceSummary.distinct_channels === 1 ? '' : 's'}
          {:else}
            Telegram provenance and professional performance details
          {/if}
        </span>
      </span>
      {#if sourceSummary}
        <span class="flex flex-wrap gap-2" aria-label="Compact source statistics">
          <Badge>{formatCompactCount(sourceSummary.totals.views)} views</Badge>
          <Badge>{formatCompactCount(sourceSummary.totals.reactions)} reactions</Badge>
          <Badge>{formatCompactCount(sourceSummary.totals.reposts)} reposts</Badge>
          {#if analytics}<Badge>{formatCompactCount(analytics.summary.totals.recorded_activity)} recorded signals</Badge>{/if}
        </span>
      {:else if analytics}
        <Badge>{formatCompactCount(analytics.summary.totals.recorded_activity)} recorded signals</Badge>
      {/if}
    </span>
  </summary>

  <div class="grid gap-8 border-t border-line px-4 py-6 sm:px-5">
    {#if isUpdating}
      <p class="m-0 text-sm font-semibold text-muted" aria-live="polite">Updating source and activity data…</p>
    {/if}

    <section class="grid gap-4" aria-labelledby="telegram-sources-title">
      <div class="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 id="telegram-sources-title" class="m-0 text-2xl font-black tracking-[-0.04em]">Observed Telegram posts</h2>
          <p class="m-0 mt-1 max-w-4xl text-sm leading-relaxed text-muted">
            Every public-crawler post we observed for this meme. Totals add only known counters; “Unknown” means Telegram did not expose a value, not zero.
          </p>
        </div>
        {#if sourcePage?.items.length}
          <p class="m-0 text-sm font-semibold text-muted">Showing {pageStart}–{pageEnd} of {sourcePage.total}</p>
        {:else if sourcePage}
          <p class="m-0 text-sm font-semibold text-muted">{sourcePage.total} posts total</p>
        {/if}
      </div>

      <nav class="flex gap-2 overflow-x-auto pb-1" aria-label="Sort Telegram source posts">
        {#each MEME_SOURCE_SORTS as option (option.value)}
          <PillLink
            size="compact"
            active={insightsParams.sourceSort === option.value}
            href={href({ sourceSort: option.value, sourceOffset: 0, sourceSnapshot: null })}
          >{option.label}</PillLink>
        {/each}
      </nav>

      {#if sourceError}
        <Notice tone="danger" role="alert" class="my-0">{sourceError} Activity analytics below may still be available.</Notice>
      {:else if sourcePage && sourcePage.total === 0}
        <div class="rounded-xl border border-dashed border-line bg-soft p-5" role="status">
          <p class="m-0 font-extrabold">No public Telegram source posts observed yet</p>
          <p class="m-0 mt-1 text-sm text-muted">This is an honest gap in the catalog, not evidence that the meme has no other origin.</p>
        </div>
      {:else if sourcePage}
        <div class="grid gap-3">
          {#if sourcePage.items.length === 0}
            <div class="rounded-xl border border-dashed border-line bg-soft p-5" role="status">
              <p class="m-0 font-extrabold">No posts on this page</p>
              <p class="m-0 mt-1 text-sm text-muted">The requested offset is past the available source list. Use Previous to return to observed posts.</p>
            </div>
          {/if}
          {#each sourcePage.items as post, index (`${post.post_url ?? post.channel_url ?? post.channel_title}:${post.published_at ?? 'unknown'}:${index}`)}
            <Card class="grid gap-4 p-4 shadow-none sm:grid-cols-[minmax(13rem,1fr)_minmax(18rem,1.3fr)] sm:items-center">
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  {#if post.channel_url}
                    <a class="font-black underline decoration-2 underline-offset-4" href={post.channel_url} target="_blank" rel="noreferrer">{post.channel_title}</a>
                  {:else}
                    <span class="font-black">{post.channel_title}</span>
                  {/if}
                  {#if !post.available}<Badge>Post unavailable</Badge>{/if}
                </div>
                {#if post.channel_username}<p class="m-0 mt-1 text-sm text-muted">@{post.channel_username}</p>{/if}
                <p class="m-0 mt-2 text-sm text-muted">Published {formatDate(post.published_at)}</p>
                {#if post.post_url}
                  <a class="mt-2 inline-block text-sm font-extrabold" href={post.post_url} target="_blank" rel="noreferrer">Open Telegram post ↗</a>
                {:else}
                  <p class="m-0 mt-2 text-sm text-muted">A public post link is unavailable.</p>
                {/if}
              </div>

              <div class="grid grid-cols-2 gap-2 sm:grid-cols-4" aria-label={`Counters for ${post.channel_title}`}>
                <div class="rounded-xl bg-soft p-3"><span class="block text-xs font-semibold uppercase tracking-wide text-muted">Views</span><strong class="tabular-nums">{formatCount(post.views)}</strong></div>
                <div class="rounded-xl bg-soft p-3"><span class="block text-xs font-semibold uppercase tracking-wide text-muted">Reactions</span><strong class="tabular-nums">{formatCount(post.reactions)}</strong></div>
                <div class="rounded-xl bg-soft p-3"><span class="block text-xs font-semibold uppercase tracking-wide text-muted">Comments</span><strong class="tabular-nums">{formatCount(post.comments)}</strong></div>
                <div class="rounded-xl bg-soft p-3"><span class="block text-xs font-semibold uppercase tracking-wide text-muted">Reposts</span><strong class="tabular-nums">{formatCount(post.reposts)}</strong></div>
                <p class="col-span-2 m-0 text-xs text-muted sm:col-span-4">
                  Interaction rate {formatRate(post.rates.interactions.value)} · Audience at publish {formatCount(post.audience.audience_at_publish)} · Views per 1,000 subscribers {formatPerThousand(post.audience.views_per_1000_subscribers)}
                </p>
              </div>
            </Card>
          {/each}
        </div>

        <div class="flex flex-wrap items-center justify-between gap-3">
          <p class="m-0 text-xs text-muted">Counters frozen at this list snapshot: {formatDate(sourcePage.snapshot_at, true)} UTC.</p>
          <nav class="flex gap-2" aria-label="Telegram source pagination">
            {#if sourcePage.offset > 0}
              <PillLink
                size="compact"
                href={href({
                  sourceOffset: Math.max(sourcePage.offset - sourcePage.limit, 0),
                  sourceSnapshot: sourcePage.snapshot_at
                })}
              >Previous</PillLink>
            {/if}
            {#if sourcePage.has_more}
              <PillLink
                size="compact"
                href={href({
                  sourceOffset: sourcePage.offset + sourcePage.limit,
                  sourceSnapshot: sourcePage.snapshot_at
                })}
              >Next posts</PillLink>
            {/if}
          </nav>
        </div>
      {/if}
    </section>

    <details id={MEME_ANALYTICS_ANCHOR} bind:open={analyticsOpen} class="rounded-xl border border-line bg-cream/60" aria-labelledby="professional-analytics-title">
      <summary class="cursor-pointer px-4 py-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent sm:px-5">
        <span class="block">
          <span class="block text-xs font-extrabold uppercase tracking-[0.16em] text-muted">For social media specialists</span>
          <span id="professional-analytics-title" class="block text-2xl font-black tracking-[-0.04em]">Professional analytics</span>
          <span class="mt-1 block max-w-4xl text-sm font-normal leading-relaxed text-muted">Trends, normalized performance, channel-audience context, and separate web/Telegram funnels.</span>
        </span>
      </summary>

      <div class="grid gap-5 border-t border-line px-4 py-5 sm:px-5">
        <div class="flex flex-wrap items-end justify-between gap-3">
          <p class="m-0 max-w-4xl text-sm leading-relaxed text-muted">
            Recorded activity counts observable signals, not unique people or reach. Impressions, inline results, comments, downloads, and subscriber counts remain separate from Recorded activity and popularity.
          </p>
          {#if analytics}<p class="m-0 text-xs text-muted">Refreshed {formatDate(analytics.refreshed_at, true)} UTC</p>{/if}
        </div>

      <nav class="flex gap-2 overflow-x-auto pb-1" aria-label="Activity history range">
        {#each MEME_ANALYTICS_WINDOWS as option (option.value)}
          <PillLink
            size="compact"
            active={insightsParams.analyticsWindow === option.value}
            href={href({
              analyticsWindow: option.value,
              sourceSnapshot: sourcePage?.snapshot_at ?? insightsParams.sourceSnapshot
            })}
          >{option.label}</PillLink>
        {/each}
      </nav>

      {#if analyticsError}
        <Notice tone="danger" role="alert" class="my-0">{analyticsError} The Telegram post list above may still be available.</Notice>
      {:else if analytics}
        {#if analytics.insufficient_history}
          <Notice class="my-0">History is still sparse. Totals are real, but trend comparisons need more observations and should be read cautiously.</Notice>
        {/if}

        <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label="Activity key performance indicators">
          <Card class="shadow-none"><p class="m-0 text-xs font-extrabold uppercase tracking-wide text-muted">Recorded activity</p><p class="mb-0 mt-2 text-3xl font-black tabular-nums">{formatCount(analytics.summary.totals.recorded_activity)}</p><p class="m-0 text-sm text-muted">{formatPerThousand(analytics.summary.average_recorded_activity_per_day)} per day</p></Card>
          <Card class="shadow-none"><p class="m-0 text-xs font-extrabold uppercase tracking-wide text-muted">7-day momentum</p><p class="mb-0 mt-2 text-3xl font-black tabular-nums">{formatSignedCount(analytics.summary.momentum.change)}</p><p class="m-0 text-sm text-muted">{formatRate(analytics.summary.momentum.change_rate)} vs previous 7 days</p></Card>
          <Card class="shadow-none"><p class="m-0 text-xs font-extrabold uppercase tracking-wide text-muted">Peak bucket</p><p class="mb-0 mt-2 text-3xl font-black tabular-nums">{analytics.summary.peak ? formatCount(analytics.summary.peak.recorded_activity) : 'Unknown'}</p><p class="m-0 text-sm text-muted">{analytics.summary.peak ? formatDate(analytics.summary.peak.bucket_start) : 'More history needed'}</p></Card>
          <Card class="shadow-none"><p class="m-0 text-xs font-extrabold uppercase tracking-wide text-muted">Current favorites</p><p class="mb-0 mt-2 text-3xl font-black tabular-nums">{formatCount(analytics.summary.current_favorites)}</p><p class="m-0 text-sm text-muted">Current state, outside the selected-period total</p></Card>
        </div>

        <MemeActivityCharts {analytics} />

        <div class="grid gap-4 xl:grid-cols-3">
          <Card class="grid content-start gap-4 shadow-none">
            <div><h3 class="m-0 text-xl font-black">Telegram performance</h3><p class="m-0 mt-1 text-sm text-muted">Current observed totals and ratio-of-sums over eligible posts.</p></div>
            <p class="m-0 rounded-xl bg-soft p-3 text-sm font-extrabold">{knownTotalsLabel(analytics.source_performance.totals)}</p>
            <dl class="m-0 grid gap-3 text-sm">
              <div><dt class="font-semibold text-muted">Reaction rate</dt><dd class="m-0 font-extrabold">{formatRate(analytics.source_performance.rates.reactions.value)} <span class="font-normal text-muted">· {coverageLabel(analytics.source_performance.rates.reactions)}</span></dd></div>
              <div><dt class="font-semibold text-muted">Comment rate</dt><dd class="m-0 font-extrabold">{formatRate(analytics.source_performance.rates.comments.value)} <span class="font-normal text-muted">· {coverageLabel(analytics.source_performance.rates.comments)}</span></dd></div>
              <div><dt class="font-semibold text-muted">Repost rate</dt><dd class="m-0 font-extrabold">{formatRate(analytics.source_performance.rates.reposts.value)} <span class="font-normal text-muted">· {coverageLabel(analytics.source_performance.rates.reposts)}</span></dd></div>
              <div><dt class="font-semibold text-muted">Combined interaction rate</dt><dd class="m-0 font-extrabold">{formatRate(analytics.source_performance.rates.interactions.value)} <span class="font-normal text-muted">· {coverageLabel(analytics.source_performance.rates.interactions)}</span></dd></div>
            </dl>
          </Card>

          <Card class="grid content-start gap-4 shadow-none">
            <div><h3 class="m-0 text-xl font-black">Channel audience context</h3><p class="m-0 mt-1 text-sm text-muted">Forward-only subscriber observations; today’s audience is never applied to historical posts.</p></div>
            <dl class="m-0 grid gap-3 text-sm">
              <div><dt class="font-semibold text-muted">Current audience known</dt><dd class="m-0 font-extrabold">{analytics.audience_change.current_known_channels} of {analytics.audience_change.total_channels} channels</dd></div>
              <div><dt class="font-semibold text-muted">Comparable channels in range</dt><dd class="m-0 font-extrabold">{formatCount(analytics.audience_change.comparable_channels)}</dd></div>
              <div><dt class="font-semibold text-muted">Known subscriber change</dt><dd class="m-0 font-extrabold">{formatSignedCount(analytics.audience_change.net_known_subscriber_change)}</dd></div>
              <div><dt class="font-semibold text-muted">Views per 1,000 subscribers</dt><dd class="m-0 font-extrabold">{formatPerThousand(analytics.source_performance.audience.views_per_1000_subscribers.value)} <span class="font-normal text-muted">· {coverageLabel(analytics.source_performance.audience.views_per_1000_subscribers)}</span></dd></div>
              <div><dt class="font-semibold text-muted">Interactions per 1,000 subscribers</dt><dd class="m-0 font-extrabold">{formatPerThousand(analytics.source_performance.audience.interactions_per_1000_subscribers.value)} <span class="font-normal text-muted">· {coverageLabel(analytics.source_performance.audience.interactions_per_1000_subscribers)}</span></dd></div>
            </dl>
          </Card>

          <Card class="grid content-start gap-4 shadow-none">
            <div><h3 class="m-0 text-xl font-black">Exposure funnels</h3><p class="m-0 mt-1 text-sm text-muted">Distinct matched exposure tokens, split by surface. These figures do not enter Recorded activity.</p></div>
            <div class="grid gap-2 rounded-xl bg-soft p-3 text-sm">
              <p class="m-0 font-extrabold">Web cards</p>
              <p class="m-0">{formatCount(analytics.exposure_funnels.web.recorded_card_impressions)} impressions · {formatCount(analytics.exposure_funnels.web.attributed_impressions)} attributed</p>
              <p class="m-0">{formatCount(analytics.exposure_funnels.web.matched_detail_clicks)} detail clicks ({formatRate(analytics.exposure_funnels.web.detail_click_rate)})</p>
              <p class="m-0">{formatCount(analytics.exposure_funnels.web.matched_high_intent_actions)} high-intent actions ({formatRate(analytics.exposure_funnels.web.high_intent_rate)})</p>
            </div>
            <div class="grid gap-2 rounded-xl bg-soft p-3 text-sm">
              <p class="m-0 font-extrabold">Telegram inline</p>
              <p class="m-0">{formatCount(analytics.exposure_funnels.telegram_inline.inline_results_served)} results served · {formatCount(analytics.exposure_funnels.telegram_inline.attributed_results_served)} attributed</p>
              <p class="m-0">{formatCount(analytics.exposure_funnels.telegram_inline.matched_chosen)} chosen ({formatRate(analytics.exposure_funnels.telegram_inline.chosen_rate)})</p>
              <p class="m-0">{formatCount(analytics.exposure_funnels.telegram_inline.matched_sent)} sent ({formatRate(analytics.exposure_funnels.telegram_inline.sent_rate)})</p>
            </div>
          </Card>
        </div>

        <p class="m-0 text-xs leading-relaxed text-muted">
          Coverage is partial whenever a counter or subscriber snapshot was unavailable. Source totals are sums of known post counters and may overlap across channels; they are neither unique viewers nor estimated reach. Downloads in this range: {formatCount(analytics.summary.totals.downloads)}.
        </p>
      {:else}
        <div class="rounded-xl border border-dashed border-line bg-soft p-5" role="status">
          <p class="m-0 font-extrabold">No professional analytics available yet</p>
          <p class="m-0 mt-1 text-sm text-muted">Data will appear as MemeExpert records activity and observes public source counters.</p>
        </div>
      {/if}
      </div>
    </details>
  </div>
</details>
