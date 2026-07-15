<script lang="ts">
  import type { AdminAnalyticsOverviewRead } from '$lib/api/types';
  import { Card, EmptyState, Notice } from '$lib/ui';
  import AnalyticsDonut from './AnalyticsDonut.svelte';
  import AnalyticsFunnel from './AnalyticsFunnel.svelte';
  import AnalyticsHeader from './AnalyticsHeader.svelte';
  import MetricGrid, { type AnalyticsMetricDefinition } from './MetricGrid.svelte';
  import AnalyticsTimeSeriesChart from './AnalyticsTimeSeriesChart.svelte';
  import { formatAnalyticsNumber } from './format';
  import { analyticsRangeFromRead, type AdminAnalyticsRangeParams } from './range';

  let {
    dashboard,
    requestedRange,
    loadError
  }: {
    dashboard: AdminAnalyticsOverviewRead | null;
    requestedRange: AdminAnalyticsRangeParams;
    loadError: string | null;
  } = $props();

  const range = $derived(analyticsRangeFromRead(dashboard?.range));
  const metricDefinitions: AnalyticsMetricDefinition[] = [
    { key: 'catalog_memes', label: 'Catalog memes', description: 'Total memes currently in the catalog.' },
    { key: 'new_memes', label: 'New memes', description: 'Memes created during this reporting window.' },
    { key: 'page_views', label: 'Page views', description: 'First-party consumer route visits.' },
    { key: 'active_users', label: 'Active users', description: 'Accounts with a recorded product event.' },
    { key: 'interactions', label: 'Interactions', description: 'Recorded product actions across surfaces.' },
    { key: 'downloads', label: 'Downloads', description: 'Meme download events.' },
    { key: 'guest_to_full_conversions', label: 'Guest → full', description: 'Recorded account upgrades or merges.' }
  ];
  const activity = $derived(
    (dashboard?.activity ?? []).map((point) => ({
      date: point.date,
      values: {
        page_views: point.page_views,
        active_users: point.active_users,
        interactions: point.interactions,
        searches: point.searches,
        downloads: point.downloads,
        new_memes: point.new_memes
      }
    }))
  );
  const sourceStats = $derived(dashboard?.source_activity ?? null);
</script>

<AnalyticsHeader
  activeSection="overview"
  currentPath="/admin/analytics"
  title="Analytics overview"
  description="Catalog growth, first-party traffic, discovery, and source signals in one UTC reporting window."
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
      label="Catalog and product activity"
      description="Page views, active accounts, interactions, and searches over the selected UTC range. Exact values follow in the table."
      points={activity}
      series={[
        { key: 'page_views', label: 'Page views', color: '#b45309' },
        { key: 'active_users', label: 'Active users', color: '#047857' },
        { key: 'interactions', label: 'Interactions', color: '#7c3aed' },
        { key: 'searches', label: 'Searches', color: '#0369a1' }
      ]}
      valueLabel="events"
    />
    <AnalyticsDonut items={dashboard.surface_mix} label="Surface mix" />
  </div>

  <div class="mt-7 grid gap-6 xl:grid-cols-[minmax(0,1.3fr)_minmax(21rem,0.7fr)]">
    <AnalyticsFunnel funnel={dashboard.discovery_funnel} />
    <Card class="grid gap-5" aria-labelledby="source-signal-heading">
      <div class="grid gap-1">
        <p class="m-0 text-xs font-black uppercase tracking-[0.16em] text-muted">Sources</p>
        <h2 id="source-signal-heading" class="m-0 text-2xl font-black tracking-[-0.04em]">Source activity</h2>
        <p class="m-0 text-sm text-muted">Latest source signals collected during this reporting window.</p>
      </div>
      <dl class="m-0 grid grid-cols-2 gap-3">
        <div class="rounded-2xl bg-soft p-4"><dt class="text-sm text-muted">Active sources</dt><dd class="m-0 mt-1 text-2xl font-black">{formatAnalyticsNumber(sourceStats?.sources)}</dd></div>
        <div class="rounded-2xl bg-soft p-4"><dt class="text-sm text-muted">New sources</dt><dd class="m-0 mt-1 text-2xl font-black">{formatAnalyticsNumber(sourceStats?.new_sources)}</dd></div>
        <div class="rounded-2xl bg-soft p-4"><dt class="text-sm text-muted">Source views</dt><dd class="m-0 mt-1 text-2xl font-black">{formatAnalyticsNumber(sourceStats?.source_views)}</dd></div>
        <div class="rounded-2xl bg-soft p-4"><dt class="text-sm text-muted">Reactions + reposts</dt><dd class="m-0 mt-1 text-2xl font-black">{formatAnalyticsNumber((sourceStats?.source_reactions ?? 0) + (sourceStats?.source_reposts ?? 0))}</dd></div>
      </dl>
    </Card>
  </div>
{:else if !loadError}
  <EmptyState title="Analytics are warming up" message="The overview will populate when the reporting service has data to return." />
{/if}
