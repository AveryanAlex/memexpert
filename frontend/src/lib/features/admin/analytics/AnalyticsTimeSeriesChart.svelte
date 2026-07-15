<script lang="ts">
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
  import { finiteNonNegative, formatAnalyticsNumber } from './format';
  import { formatUtcDate } from './range';

  export interface AnalyticsTimeSeriesPoint {
    date: string;
    values: Record<string, number>;
  }

  export interface AnalyticsTimeSeriesDefinition {
    key: string;
    label: string;
    color?: string;
  }

  type ChartDatum = {
    date: string;
    timestamp: number;
    value: number;
    seriesKey: string;
    seriesLabel: string;
    color: string;
  };

  let {
    label,
    description,
    points,
    series,
    valueLabel = 'events',
    loading = false
  }: {
    label: string;
    description: string;
    points: AnalyticsTimeSeriesPoint[];
    series: AnalyticsTimeSeriesDefinition[];
    valueLabel?: string;
    loading?: boolean;
  } = $props();

  const populatedSeries = $derived(series.filter((definition) => points.some((point) => finiteNonNegative(point.values[definition.key]) > 0)));
  const plottedSeries = $derived(populatedSeries.length > 0 ? populatedSeries : series);
  const chartData: ChartDatum[] = $derived(
    plottedSeries.flatMap((definition, seriesIndex) =>
      points.map((point, pointIndex) => ({
        date: point.date,
        timestamp: timestampFor(point.date, pointIndex),
        value: finiteNonNegative(point.values[definition.key]),
        seriesKey: definition.key,
        seriesLabel: definition.label,
        color: definition.color ?? chartSeriesPalette[seriesIndex % chartSeriesPalette.length]
      }))
    )
  );
  const canPlot = $derived(points.length >= 2 && chartData.length >= 2 && chartData.some((point) => point.value > 0));
  const xDomain = $derived.by(() => domainFor(chartData.map((point) => point.timestamp), 86_400_000));
  const yDomain = $derived.by(() => domainFor(chartData.map((point) => point.value), 1, true));

  function timestampFor(date: string, fallbackIndex: number): number {
    const timestamp = Date.parse(`${date.slice(0, 10)}T00:00:00Z`);
    return Number.isFinite(timestamp) ? timestamp : fallbackIndex * 86_400_000;
  }

  function domainFor(values: number[], padding: number, startsAtZero = false): [number, number] | undefined {
    if (values.length === 0) return undefined;
    const min = startsAtZero ? 0 : Math.min(...values);
    const max = Math.max(...values);
    return min === max ? [Math.max(0, min - padding), max + padding] : [min, max];
  }
</script>

{#snippet footer()}
  <span class="rounded-full border border-line bg-paper px-3 py-2">Earlier → latest</span>
  {#each plottedSeries as definition, index (definition.key)}
    <span class="inline-flex items-center gap-2 rounded-full border border-line bg-paper px-3 py-2">
      <span class="size-2.5 rounded-full" style:background={definition.color ?? chartSeriesPalette[index % chartSeriesPalette.length]}></span>
      {definition.label}
    </span>
  {/each}
{/snippet}

<section class="grid gap-4" aria-label={label}>
  <ChartFrame
    {label}
    {description}
    {loading}
    loadingLabel={`Loading ${label.toLowerCase()}`}
    empty={!canPlot}
    emptyTitle="Not enough activity to plot yet"
    emptyMessage="The exact values will appear in the table when activity is recorded."
    footer={canPlot ? footer : undefined}
  >
    {#if canPlot}
      <p class="sr-only">The vertical axis shows {valueLabel}. The horizontal axis moves from the earliest to latest date in the selected UTC range.</p>
      <LayerChart
        data={chartData}
        x="timestamp"
        y="value"
        {xDomain}
        {yDomain}
        xPadding={[10, 10]}
        yPadding={[12, 12]}
        padding={{ top: 12, right: 18, bottom: 18, left: 48 }}
        tooltip={{ mode: 'quadtree' }}
        ssr
      >
        <LayerChartSvg title={label}>
          <LayerChartGrid y={{ class: 'stroke-ink/10' }} yTicks={4} />
          <LayerChartAxis
            placement="left"
            rule={{ class: 'stroke-ink/20' }}
            ticks={4}
            classes={{ tick: 'stroke-ink/20', tickLabel: 'fill-muted text-[11px] font-bold' }}
          />
          {#each plottedSeries as definition, seriesIndex (definition.key)}
            {@const seriesData = chartData.filter((point) => point.seriesKey === definition.key)}
            {@const color = definition.color ?? chartSeriesPalette[seriesIndex % chartSeriesPalette.length]}
            <LayerChartSpline
              data={seriesData}
              x="timestamp"
              y="value"
              stroke={color}
              strokeWidth={3}
              fill="none"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
            <LayerChartPoints data={seriesData} x="timestamp" y="value" r={3.5} fill={color} stroke="var(--color-paper)" strokeWidth={2} />
          {/each}
        </LayerChartSvg>
        <LayerChartTooltip.Root
          variant="none"
          classes={{ container: 'rounded-2xl border border-line bg-paper/95 px-3 py-2 text-sm text-ink shadow-warm-lg' }}
        >
          {#snippet children({ data }: { data: ChartDatum })}
            <div class="grid gap-1">
              <p class="m-0 font-extrabold">{formatUtcDate(data.date)}</p>
              <p class="m-0 flex items-center gap-2 font-extrabold">
                <span class="size-2.5 rounded-full" style:background={data.color}></span>
                {data.seriesLabel}: {formatAnalyticsNumber(data.value)} {valueLabel}
              </p>
            </div>
          {/snippet}
        </LayerChartTooltip.Root>
      </LayerChart>
    {/if}
  </ChartFrame>

  {#if points.length > 0}
    <div class="overflow-x-auto rounded-2xl border border-line bg-paper">
      <table class="w-full min-w-[42rem] border-collapse text-left text-sm">
        <caption class="sr-only">Exact {label.toLowerCase()} values by date.</caption>
        <thead class="bg-soft text-muted">
          <tr>
            <th class="px-4 py-3 font-black" scope="col">Date</th>
            {#each series as definition (definition.key)}
              <th class="px-4 py-3 font-black" scope="col">{definition.label}</th>
            {/each}
          </tr>
        </thead>
        <tbody>
          {#each points as point (`${point.date}:${JSON.stringify(point.values)}`)}
            <tr class="border-t border-line">
              <th class="px-4 py-3 font-extrabold" scope="row">{formatUtcDate(point.date)}</th>
              {#each series as definition (definition.key)}
                <td class="px-4 py-3 tabular-nums">{formatAnalyticsNumber(point.values[definition.key])}</td>
              {/each}
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>
