<script lang="ts">
  import type { AdminAnalyticsContentRead } from '$lib/api/types';
  import { EmptyState, Notice } from '$lib/ui';
  import AnalyticsBreakdownChart from './AnalyticsBreakdownChart.svelte';
  import AnalyticsHeader from './AnalyticsHeader.svelte';
  import MetricGrid, { type AnalyticsMetricDefinition } from './MetricGrid.svelte';
  import AnalyticsTimeSeriesChart from './AnalyticsTimeSeriesChart.svelte';
  import { analyticsRangeFromRead, type AdminAnalyticsRangeParams } from './range';

  let {
    dashboard,
    requestedRange,
    loadError
  }: {
    dashboard: AdminAnalyticsContentRead | null;
    requestedRange: AdminAnalyticsRangeParams;
    loadError: string | null;
  } = $props();

  const range = $derived(analyticsRangeFromRead(dashboard?.range));
  const metricDefinitions: AnalyticsMetricDefinition[] = [
    { key: 'catalog_memes', label: 'Catalog memes', description: 'Total catalog size at the end of the selected range.' },
    { key: 'new_memes', label: 'New memes', description: 'Meme records created during the selected range.' },
    { key: 'public_memes', label: 'Public memes', description: 'Publicly visible catalog records.' },
    { key: 'private_memes', label: 'Private memes', description: 'Private catalog records.' },
    { key: 'nsfw_memes', label: 'NSFW memes', description: 'Records currently marked NSFW.' },
    { key: 'seo_pages', label: 'SEO pages', description: 'Meme SEO pages available at period end.' },
    { key: 'active_sources', label: 'Active sources', description: 'Active, unpaused configured source channels at period end.' },
    { key: 'new_sources', label: 'New sources', description: 'Configured source channels created during the selected range.' },
    { key: 'source_views', label: 'Source views', description: 'Snapshot-to-snapshot source view deltas.' },
    { key: 'source_reactions', label: 'Source reactions', description: 'Snapshot-to-snapshot reaction deltas.' },
    { key: 'source_reposts', label: 'Source reposts', description: 'Snapshot-to-snapshot repost deltas.' }
  ];
  const catalogGrowth = $derived(
    (dashboard?.catalog_growth ?? []).map((point) => ({ date: point.date, values: { new_memes: point.new_memes } }))
  );
  const sourceEngagement = $derived(
    (dashboard?.source_engagement ?? []).map((point) => ({
      date: point.date,
      values: {
        source_views: point.source_views,
        source_reactions: point.source_reactions,
        source_reposts: point.source_reposts
      }
    }))
  );
</script>

<AnalyticsHeader
  activeSection="content"
  currentPath="/admin/analytics/content"
  title="Content & sources"
  description="Inspect catalog growth, classification coverage, processing mix, source health, and observed source engagement deltas."
  {range}
  {requestedRange}
/>

{#if loadError}
  <Notice tone="danger" role="alert">{loadError}</Notice>
{/if}

{#if dashboard}
  <MetricGrid metrics={dashboard.metrics} definitions={metricDefinitions} />

  <div class="mt-7 grid gap-6 xl:grid-cols-2">
    <AnalyticsTimeSeriesChart
      label="Catalog growth"
      description="New meme records created by UTC date. Total catalog size is shown in the metric card above."
      points={catalogGrowth}
      series={[{ key: 'new_memes', label: 'New memes', color: '#b45309' }]}
      valueLabel="memes"
    />
    <AnalyticsTimeSeriesChart
      label="Source engagement deltas"
      description="Observed snapshot-to-snapshot changes; a source's first snapshot is treated as a baseline, not new engagement."
      points={sourceEngagement}
      series={[
        { key: 'source_views', label: 'Views', color: '#0369a1' },
        { key: 'source_reactions', label: 'Reactions', color: '#047857' },
        { key: 'source_reposts', label: 'Reposts', color: '#7c3aed' }
      ]}
      valueLabel="signals"
    />
  </div>

  <div class="mt-7 grid gap-6 xl:grid-cols-2">
    <AnalyticsBreakdownChart label="Media types" description="Catalog distribution by media type." items={dashboard.media_types} />
    <AnalyticsBreakdownChart label="Languages" description="Catalog distribution by classified content language." items={dashboard.languages} />
    <AnalyticsBreakdownChart label="Visibility" description="Catalog distribution by current visibility." items={dashboard.visibility} />
    <AnalyticsBreakdownChart label="Processing mix" description="Current processing status across catalog files and pipeline work." items={dashboard.processing} />
    <AnalyticsBreakdownChart label="Source health" description="Current health categories for configured sources." items={dashboard.source_health} />
  </div>
{:else if !loadError}
  <EmptyState title="Content analytics are warming up" message="Catalog and source breakdowns will appear when the reporting service returns data." />
{/if}
