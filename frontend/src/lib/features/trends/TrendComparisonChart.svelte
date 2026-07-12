<script lang="ts">
  import type { PublicTrendComparisonSeriesRead } from '$lib/api/types';
  import {
    ChartFrame,
    LayerChart,
    LayerChartAxis,
    LayerChartGrid,
    LayerChartPoints,
    LayerChartSpline,
    LayerChartSvg,
    LayerChartTooltip,
    chartSeriesPalette
  } from '$lib/ui/chart';

  let { series }: { series: PublicTrendComparisonSeriesRead[] } = $props();

  type TrendComparisonDatum = {
    progress: number;
    activity: number;
    sourceActivity: number;
    memeExpertActivity: number;
    observedAt: string | null;
    seriesTitle: string;
    color: string;
  };

  type TrendComparisonSeries = PublicTrendComparisonSeriesRead & {
    color: string;
    chartData: TrendComparisonDatum[];
  };

  const plottedSeries: TrendComparisonSeries[] = $derived(
    series
      .map((item, seriesIndex) => ({
        ...item,
        color: chartSeriesPalette[seriesIndex % chartSeriesPalette.length],
        chartData: item.points.map((point, pointIndex) => ({
          progress: item.points.length <= 1 ? 0.5 : pointIndex / (item.points.length - 1),
          activity: recordedActivity(point),
          sourceActivity: sourceActivity(point),
          memeExpertActivity: memeExpertActivity(point),
          observedAt: point.observed_at,
          seriesTitle: item.title,
          color: chartSeriesPalette[seriesIndex % chartSeriesPalette.length]
        }))
      }))
      .filter((item) => item.chartData.length >= 2 && item.chartData.some((point) => point.activity > 0))
  );
  const chartData: TrendComparisonDatum[] = $derived(plottedSeries.flatMap((item) => item.chartData));
  const recordedActivityDescription =
    'Recorded activity adds original-source views, reactions, and reposts to MemeExpert views, sends, saves, and favorites. It counts signals, not unique people.';
  const numberFormatter = new Intl.NumberFormat('en');
  const yDomain = $derived.by(() => {
    const values = chartData.map((point) => point.activity);
    if (values.length === 0) return undefined;

    const min = Math.min(...values);
    const max = Math.max(...values);

    return min === max ? [min - 1, max + 1] : undefined;
  });

  function formatObservedAt(raw: string | null): string {
    if (!raw) return 'This week';

    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return 'This week';

    return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }).format(date);
  }

  /**
   * This is the same unweighted signal count used by aggregate history and
   * timeline cards. It never substitutes a platform field for a source field.
   */
  function recordedActivity(point: PublicTrendComparisonSeriesRead['points'][number]): number {
    return sourceActivity(point) + memeExpertActivity(point);
  }

  function sourceActivity(point: PublicTrendComparisonSeriesRead['points'][number]): number {
    return count(point.source_views) + count(point.source_reactions) + count(point.source_reposts);
  }

  function memeExpertActivity(point: PublicTrendComparisonSeriesRead['points'][number]): number {
    return count(point.platform_views) + count(point.platform_sends) + count(point.platform_saves) + count(point.platform_likes);
  }

  function count(value: number | null | undefined): number {
    return typeof value === 'number' && Number.isFinite(value) ? Math.max(value, 0) : 0;
  }

  function formatCount(value: number): string {
    return numberFormatter.format(value);
  }
</script>

{#snippet footer()}
  <span class="rounded-full border border-line bg-paper px-3 py-2">Vertical scale: recorded activity</span>
  <span class="rounded-full border border-line bg-paper px-3 py-2">Earlier → latest</span>
  {#each plottedSeries as item (`legend:${item.kind}:${item.value}`)}
    <span class="inline-flex items-center gap-2 rounded-full border border-line bg-paper px-3 py-2">
      <span class="h-2.5 w-2.5 rounded-full" style:background={item.color}></span>
      {item.title}
    </span>
  {/each}
{/snippet}

<ChartFrame
  label="Recorded activity comparison"
  description={`${recordedActivityDescription} Each line moves from earlier to later activity. A readable table follows below.`}
  empty={plottedSeries.length === 0}
  emptyTitle="More activity will appear here soon"
  emptyMessage="Pick items with recorded activity and come back as people discover them."
  size="tall"
  footer={plottedSeries.length > 0 ? footer : undefined}
>
  <LayerChart
    data={chartData}
    x="progress"
    y="activity"
    xDomain={[0, 1]}
    {yDomain}
    xPadding={[10, 10]}
    yPadding={[12, 12]}
    padding={{ top: 12, right: 18, bottom: 18, left: 48 }}
    tooltip={{ mode: 'quadtree' }}
    ssr
  >
    <LayerChartSvg title="Recorded activity comparison">
      <LayerChartGrid y={{ class: 'stroke-ink/10' }} yTicks={4} />
      <LayerChartAxis
        placement="left"
        rule={{ class: 'stroke-ink/20' }}
        ticks={4}
        classes={{ tick: 'stroke-ink/20', tickLabel: 'fill-muted text-[11px] font-bold' }}
      />
      {#each plottedSeries as item (`series:${item.kind}:${item.value}`)}
        <LayerChartSpline
          data={item.chartData}
          x="progress"
          y="activity"
          stroke={item.color}
          strokeWidth={3}
          fill="none"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
        <LayerChartPoints
          data={item.chartData}
          x="progress"
          y="activity"
          r={4}
          fill={item.color}
          stroke="var(--color-paper)"
          strokeWidth={2}
        />
      {/each}
    </LayerChartSvg>

    <LayerChartTooltip.Root
      variant="none"
      classes={{ container: 'rounded-2xl border border-line bg-paper/95 px-3 py-2 text-sm text-ink shadow-warm-lg' }}
    >
      {#snippet children({ data }: { data: TrendComparisonDatum })}
        <div class="grid gap-1">
          <p class="m-0 flex items-center gap-2 font-extrabold">
            <span class="h-2.5 w-2.5 rounded-full" style:background={data.color}></span>
            {data.seriesTitle}
          </p>
          <p class="m-0 text-muted">Activity recorded {formatObservedAt(data.observedAt)}</p>
          <p class="m-0 font-extrabold">Recorded activity: {formatCount(data.activity)} signals</p>
          <p class="m-0 text-muted">Original sources: {formatCount(data.sourceActivity)} · MemeExpert: {formatCount(data.memeExpertActivity)}</p>
        </div>
      {/snippet}
    </LayerChartTooltip.Root>
  </LayerChart>
</ChartFrame>
