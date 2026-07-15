<script lang="ts">
  import type {
    AdminAnalyticsEngagementRead,
    AdminAnalyticsSearchQuerySort,
    AdminAnalyticsSearchQueryDetailRead,
    AdminAnalyticsSearchQueryPageRead
  } from '$lib/api/types';
  import { Card, EmptyState, Notice } from '$lib/ui';
  import AnalyticsBreakdownChart from './AnalyticsBreakdownChart.svelte';
  import AnalyticsDonut from './AnalyticsDonut.svelte';
  import AnalyticsHeader from './AnalyticsHeader.svelte';
  import MetricGrid, { type AnalyticsMetricDefinition } from './MetricGrid.svelte';
  import AnalyticsTimeSeriesChart from './AnalyticsTimeSeriesChart.svelte';
  import { formatAnalyticsNumber } from './format';
  import { adminAnalyticsHref, analyticsRangeFromRead, type AdminAnalyticsRangeParams } from './range';

  let {
    dashboard,
    searchQueries,
    queryDetail,
    selectedQueryKey,
    offset,
    sort,
    requestedRange,
    loadError
  }: {
    dashboard: AdminAnalyticsEngagementRead | null;
    searchQueries: AdminAnalyticsSearchQueryPageRead | null;
    queryDetail: AdminAnalyticsSearchQueryDetailRead | null;
    selectedQueryKey: string | null;
    offset: number;
    sort: AdminAnalyticsSearchQuerySort;
    requestedRange: AdminAnalyticsRangeParams;
    loadError: string | null;
  } = $props();

  const range = $derived(analyticsRangeFromRead(dashboard?.range ?? searchQueries?.range ?? queryDetail?.range));
  const metricDefinitions: AnalyticsMetricDefinition[] = [
    { key: 'interactions', label: 'Interactions', description: 'All recorded engagement events.' },
    { key: 'searches', label: 'Searches', description: 'Initial non-empty search requests.' },
    { key: 'zero_result_searches', label: 'Zero-result searches', description: 'Searches with no matching result.', inverseChange: true },
    { key: 'zero_result_rate', label: 'Zero-result rate', description: 'Share of searches returning zero matches.', format: 'percent', inverseChange: true },
    { key: 'average_search_latency_ms', label: 'Search latency', description: 'Average recorded search latency.', format: 'milliseconds', inverseChange: true },
    { key: 'detail_clicks', label: 'Detail clicks', description: 'Meme detail opens attributed to discovery.' },
    { key: 'downloads', label: 'Downloads', description: 'Download actions after discovery.' },
    { key: 'sends', label: 'Sends', description: 'Recorded send or inline-send actions.' },
    { key: 'saves', label: 'Saves', description: 'Save and favorite actions.' },
    { key: 'shares', label: 'Shares', description: 'Recorded share actions.' }
  ];
  const activity = $derived(
    (dashboard?.activity ?? []).map((point) => ({
      date: point.date,
      values: {
        interactions: point.interactions,
        searches: point.searches,
        zero_result_searches: point.zero_result_searches,
        detail_clicks: point.detail_clicks,
        downloads: point.downloads,
        sends: point.sends,
        saves: point.saves,
        shares: point.shares
      }
    }))
  );
  const queryItems = $derived(searchQueries?.items ?? dashboard?.top_search_queries ?? []);
  const nextOffset = $derived(searchQueries && offset + searchQueries.limit < searchQueries.total ? offset + searchQueries.limit : null);
  const previousOffset = $derived(offset > 0 ? Math.max(0, offset - (searchQueries?.limit ?? 50)) : null);
  const querySorts: Array<{ value: AdminAnalyticsSearchQuerySort; label: string; detail: string }> = [
    { value: 'searches', label: 'Popular', detail: 'Most searches' },
    { value: 'niche', label: 'Niche', detail: 'Lower-volume, high-intent queries' },
    { value: 'zero_result_rate', label: 'No-result', detail: 'Highest zero-result rate' },
    { value: 'downloads', label: 'Downloads', detail: 'Most downloads after search' }
  ];
</script>

<AnalyticsHeader
  activeSection="engagement"
  currentPath="/admin/analytics/engagement"
  title="Engagement & search"
  description="See how people search, discover memes, and take actions across product surfaces. Raw query text is visible only inside this admin workspace."
  {range}
  {requestedRange}
/>

{#if loadError}
  <Notice tone="danger" role="alert">{loadError}</Notice>
{/if}

{#if dashboard}
  <MetricGrid metrics={dashboard.metrics} definitions={metricDefinitions} />

  <div class="mt-7 grid gap-6 2xl:grid-cols-[minmax(0,1.55fr)_minmax(20rem,0.85fr)]">
    <AnalyticsTimeSeriesChart
      label="Search and interaction activity"
      description="Searches, zero-result searches, detail opens, and downloads over the selected UTC range."
      points={activity}
      series={[
        { key: 'interactions', label: 'Interactions', color: '#7c3aed' },
        { key: 'searches', label: 'Searches', color: '#0369a1' },
        { key: 'zero_result_searches', label: 'Zero-result searches', color: '#be123c' },
        { key: 'detail_clicks', label: 'Detail clicks', color: '#b45309' },
        { key: 'downloads', label: 'Downloads', color: '#047857' }
      ]}
    />
    <AnalyticsDonut items={dashboard.surface_mix} label="Engagement by surface" />
  </div>

  <div class="mt-7 grid gap-6 xl:grid-cols-2">
    <AnalyticsBreakdownChart
      label="Interactions by type"
      description="The event types contributing to recorded engagement in the selected range."
      items={dashboard.interactions_by_type}
      emptyTitle="No interaction events yet"
      emptyMessage="Engagement event types will appear as people use the product."
    />
    <Card class="grid gap-4" aria-labelledby="top-query-heading">
      <div class="grid gap-1">
        <p class="m-0 text-xs font-black uppercase tracking-[0.16em] text-muted">Search quality</p>
        <h2 id="top-query-heading" class="m-0 text-2xl font-black tracking-[-0.04em]">Top search queries</h2>
        <p class="m-0 text-sm text-muted">Prioritize frequent no-result and high-intent queries for catalog and relevance work.</p>
      </div>
      {#if dashboard.top_search_queries.length > 0}
        <div class="overflow-x-auto rounded-2xl border border-line">
          <table class="w-full min-w-[37rem] border-collapse text-left text-sm">
            <caption class="sr-only">Top raw search queries with discovery outcomes.</caption>
            <thead class="bg-soft text-muted"><tr><th class="px-3 py-3 font-black">Query</th><th class="px-3 py-3 font-black">Searches</th><th class="px-3 py-3 font-black">Zero-result rate</th><th class="px-3 py-3 font-black">Downloads</th></tr></thead>
            <tbody>
              {#each dashboard.top_search_queries.slice(0, 8) as item (item.query_key)}
                <tr class="border-t border-line"><th class="max-w-[16rem] truncate px-3 py-3 font-extrabold" scope="row" title={item.query}><a class="underline decoration-2 underline-offset-4" href={adminAnalyticsHref('/admin/analytics/engagement', range ?? requestedRange, { query_key: item.query_key, offset, sort })}>{item.query}</a></th><td class="px-3 py-3 tabular-nums">{formatAnalyticsNumber(item.searches)}</td><td class="px-3 py-3 tabular-nums">{formatAnalyticsNumber(item.zero_result_rate, 'percent')}</td><td class="px-3 py-3 tabular-nums">{formatAnalyticsNumber(item.downloads)}</td></tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <p class="m-0 rounded-2xl border border-dashed border-line bg-soft p-4 text-sm text-muted">No raw queries were recorded in this reporting window.</p>
      {/if}
    </Card>
  </div>
{/if}

<section class="mt-7 grid gap-4" aria-labelledby="query-explorer-heading">
  <div class="flex flex-wrap items-end justify-between gap-3">
    <div>
      <p class="m-0 text-xs font-black uppercase tracking-[0.16em] text-muted">Admin-only raw query data</p>
      <h2 id="query-explorer-heading" class="m-0 text-3xl font-black tracking-[-0.05em]">Search query explorer</h2>
      <p class="m-0 mt-1 text-sm text-muted">Open a query to see anonymous meme outcomes; no viewer identifiers are shown.</p>
    </div>
    {#if searchQueries}<p class="m-0 text-sm font-extrabold text-muted">{formatAnalyticsNumber(searchQueries.total)} queries</p>{/if}
  </div>

  <nav class="flex flex-wrap gap-2" aria-label="Search query sort mode">
    {#each querySorts as option (option.value)}
      {@const active = option.value === sort}
      <a
        href={adminAnalyticsHref('/admin/analytics/engagement', range ?? requestedRange, { sort: option.value, offset, query_key: selectedQueryKey })}
        aria-current={active ? 'true' : undefined}
        title={option.detail}
        class={active
          ? 'rounded-full bg-ink px-3 py-2 text-sm font-extrabold text-paper no-underline'
          : 'rounded-full border border-line bg-paper px-3 py-2 text-sm font-extrabold text-ink no-underline hover:bg-soft'}
      >{option.label}</a>
    {/each}
  </nav>

  {#if queryItems.length > 0}
    <div class="overflow-x-auto rounded-3xl border border-line bg-paper">
      <table class="w-full min-w-[58rem] border-collapse text-left text-sm">
        <caption class="sr-only">Raw search queries and their anonymous discovery outcomes.</caption>
        <thead class="bg-soft text-muted"><tr><th class="px-4 py-3 font-black">Query</th><th class="px-4 py-3 font-black">Searches</th><th class="px-4 py-3 font-black">Zero-result rate</th><th class="px-4 py-3 font-black">Average latency</th><th class="px-4 py-3 font-black">Detail clicks</th><th class="px-4 py-3 font-black">Downloads</th><th class="px-4 py-3 font-black"><span class="sr-only">Actions</span></th></tr></thead>
        <tbody>
          {#each queryItems as item (item.query_key)}
            <tr class="border-t border-line align-top">
              <th class="max-w-[22rem] px-4 py-3 font-extrabold" scope="row"><span class="block break-words">{item.query}</span></th>
              <td class="px-4 py-3 tabular-nums">{formatAnalyticsNumber(item.searches)}</td>
              <td class="px-4 py-3 tabular-nums">{formatAnalyticsNumber(item.zero_result_rate, 'percent')}</td>
              <td class="px-4 py-3 tabular-nums">{formatAnalyticsNumber(item.average_latency_ms, 'milliseconds')}</td>
              <td class="px-4 py-3 tabular-nums">{formatAnalyticsNumber(item.detail_clicks)}</td>
              <td class="px-4 py-3 tabular-nums">{formatAnalyticsNumber(item.downloads)}</td>
              <td class="px-4 py-3"><a class="whitespace-nowrap rounded-xl border border-line bg-paper px-3 py-2 text-sm font-extrabold text-ink no-underline hover:bg-soft" href={adminAnalyticsHref('/admin/analytics/engagement', range ?? requestedRange, { query_key: item.query_key, offset, sort })}>View outcomes</a></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if searchQueries}
      <nav class="flex flex-wrap items-center justify-between gap-3" aria-label="Search query pagination">
        {#if previousOffset !== null}
          <a class="rounded-xl border border-line bg-paper px-3 py-2 text-sm font-extrabold text-ink no-underline hover:bg-soft" href={adminAnalyticsHref('/admin/analytics/engagement', range ?? requestedRange, { offset: previousOffset, sort })}>Previous queries</a>
        {:else}<span class="rounded-xl border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">Previous queries</span>{/if}
        <span class="text-sm text-muted">{formatAnalyticsNumber(offset + 1)}–{formatAnalyticsNumber(Math.min(offset + searchQueries.limit, searchQueries.total))}</span>
        {#if nextOffset !== null}
          <a class="rounded-xl border border-ink bg-ink px-3 py-2 text-sm font-extrabold text-paper no-underline hover:opacity-85" href={adminAnalyticsHref('/admin/analytics/engagement', range ?? requestedRange, { offset: nextOffset, sort })}>Next queries</a>
        {:else}<span class="rounded-xl border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">Next queries</span>{/if}
      </nav>
    {/if}
  {:else if !loadError}
    <EmptyState title="No queries for this range" message="Try a broader date range once search activity has been recorded." />
  {/if}

  {#if selectedQueryKey && queryDetail}
    <Card class="grid gap-4" aria-labelledby="query-outcomes-heading">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div class="grid gap-1"><p class="m-0 text-xs font-black uppercase tracking-[0.16em] text-muted">Selected query</p><h3 id="query-outcomes-heading" class="m-0 break-words text-2xl font-black tracking-[-0.04em]">{queryDetail.query}</h3><p class="m-0 text-sm text-muted">{formatAnalyticsNumber(queryDetail.searches)} searches · {formatAnalyticsNumber(queryDetail.zero_result_rate, 'percent')} zero-result rate · {formatAnalyticsNumber(queryDetail.average_latency_ms, 'milliseconds')} average latency</p></div>
        <a class="rounded-xl border border-line bg-paper px-3 py-2 text-sm font-extrabold text-ink no-underline hover:bg-soft" href={adminAnalyticsHref('/admin/analytics/engagement', range ?? requestedRange, { offset, sort })}>Close outcomes</a>
      </div>
      {#if queryDetail.meme_outcomes.length > 0}
        <div class="overflow-x-auto rounded-2xl border border-line"><table class="w-full min-w-[52rem] border-collapse text-left text-sm"><caption class="sr-only">Anonymous outcomes for the selected search query.</caption><thead class="bg-soft text-muted"><tr><th class="px-4 py-3 font-black">Meme ID</th><th class="px-4 py-3 font-black">Interactions</th><th class="px-4 py-3 font-black">Detail clicks</th><th class="px-4 py-3 font-black">Downloads</th><th class="px-4 py-3 font-black">Saves</th><th class="px-4 py-3 font-black">Shares</th></tr></thead><tbody>{#each queryDetail.meme_outcomes as outcome (outcome.meme_id)}<tr class="border-t border-line"><th class="px-4 py-3 font-mono text-xs font-bold" scope="row">{outcome.meme_id}</th><td class="px-4 py-3 tabular-nums">{formatAnalyticsNumber(outcome.interactions)}</td><td class="px-4 py-3 tabular-nums">{formatAnalyticsNumber(outcome.detail_clicks)}</td><td class="px-4 py-3 tabular-nums">{formatAnalyticsNumber(outcome.downloads)}</td><td class="px-4 py-3 tabular-nums">{formatAnalyticsNumber(outcome.saves)}</td><td class="px-4 py-3 tabular-nums">{formatAnalyticsNumber(outcome.shares)}</td></tr>{/each}</tbody></table></div>
      {:else}
        <p class="m-0 rounded-2xl border border-dashed border-line bg-soft p-4 text-sm text-muted">No attributable meme outcomes were recorded for this query in the selected range.</p>
      {/if}
    </Card>
  {:else if selectedQueryKey && !loadError}
    <Notice tone="danger" role="alert">The selected query could not be loaded.</Notice>
  {/if}
</section>
