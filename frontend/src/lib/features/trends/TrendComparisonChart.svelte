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
    value: number;
    label: string;
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
      .filter((item) => item.points.length >= 2)
      .map((item, seriesIndex) => ({
        ...item,
        color: chartSeriesPalette[seriesIndex % chartSeriesPalette.length],
        chartData: item.points.map((point, pointIndex) => ({
          progress: item.points.length <= 1 ? 0.5 : pointIndex / (item.points.length - 1),
          value: point.value,
          label: point.label,
          observedAt: point.observed_at,
          seriesTitle: item.title,
          color: chartSeriesPalette[seriesIndex % chartSeriesPalette.length]
        }))
      }))
  );
  const chartData: TrendComparisonDatum[] = $derived(plottedSeries.flatMap((item) => item.chartData));
  const yDomain = $derived.by(() => {
    const values = chartData.map((point) => point.value);
    if (values.length === 0) return undefined;

    const min = Math.min(...values);
    const max = Math.max(...values);

    return min === max ? [min - 1, max + 1] : undefined;
  });

  function formatObservedAt(raw: string | null): string {
    if (!raw) return 'current window';

    return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric' }).format(new Date(raw));
  }
</script>

{#snippet footer()}
  {#each plottedSeries as item (`legend:${item.kind}:${item.value}`)}
    <span class="inline-flex items-center gap-2 rounded-full border border-line bg-paper px-3 py-2">
      <span class="h-2.5 w-2.5 rounded-full" style:background={item.color}></span>
      {item.title}
    </span>
  {/each}
{/snippet}

<ChartFrame
  label="Trend comparison line chart"
  description="Compare available real trend points across the selected memes, tags, and templates."
  empty={plottedSeries.length === 0}
  emptyTitle="No comparable history yet"
  emptyMessage="Full comparison lines require at least two real points per item. Current-only aggregate fallbacks remain available in the table."
  size="tall"
  footer={plottedSeries.length > 0 ? footer : undefined}
>
  <LayerChart
    data={chartData}
    x="progress"
    y="value"
    xDomain={[0, 1]}
    {yDomain}
    xPadding={[10, 10]}
    yPadding={[12, 12]}
    padding={{ top: 12, right: 18, bottom: 34, left: 48 }}
    tooltip={{ mode: 'quadtree' }}
    ssr
  >
    <LayerChartSvg title="Trend comparison line chart">
      <LayerChartGrid y={{ class: 'stroke-ink/10' }} yTicks={4} />
      <LayerChartAxis
        placement="left"
        rule={{ class: 'stroke-ink/20' }}
        ticks={4}
        classes={{ tick: 'stroke-ink/20', tickLabel: 'fill-muted text-[11px] font-bold' }}
      />
      <LayerChartAxis
        placement="bottom"
        rule={{ class: 'stroke-ink/20' }}
        ticks={[0, 0.5, 1]}
        classes={{ tick: 'stroke-ink/20', tickLabel: 'fill-muted text-[11px] font-bold' }}
      />
      {#each plottedSeries as item (`series:${item.kind}:${item.value}`)}
        <LayerChartSpline
          data={item.chartData}
          x="progress"
          y="value"
          stroke={item.color}
          strokeWidth={3}
          fill="none"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
        <LayerChartPoints
          data={item.chartData}
          x="progress"
          y="value"
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
          <p class="m-0 text-muted">{data.label} · {formatObservedAt(data.observedAt)}</p>
          <p class="m-0 font-extrabold tabular-nums">{data.value.toFixed(1)}</p>
        </div>
      {/snippet}
    </LayerChartTooltip.Root>
  </LayerChart>
</ChartFrame>
