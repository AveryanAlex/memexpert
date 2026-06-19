<script lang="ts">
  import type { PublicTrendAggregatePointRead, PublicTrendSummaryRead } from '$lib/api/types';
  import {
    ChartFrame,
    LayerChart,
    LayerChartAxis,
    LayerChartGrid,
    LayerChartPoints,
    LayerChartSpline,
    LayerChartSvg,
    LayerChartTooltip
  } from '$lib/ui/chart';

  let { summary }: { summary: PublicTrendSummaryRead } = $props();

  type AggregateDatum = {
    observedAtMs: number;
    observedAt: string | null;
    value: number;
    metric: string;
    label: string;
    memeCount: number;
    snapshotCount: number;
  };

  const numberFormatter = new Intl.NumberFormat('en');
  const lineColor = '#b45309';
  const visiblePoints = $derived(summary.points ?? []);
  const chartData: AggregateDatum[] = $derived(
    visiblePoints
      .map((point) => pointToDatum(point))
      .filter((point): point is AggregateDatum => point !== null)
  );
  const canRenderLine = $derived(visiblePoints.length >= 2 && chartData.length >= 2);
  const metricLabel = $derived(visiblePoints.at(-1)?.label ?? 'Aggregate popularity score');
  const firstObservedAt = $derived(chartData.at(0)?.observedAt ?? visiblePoints.at(0)?.observed_at ?? null);
  const lastObservedAt = $derived(chartData.at(-1)?.observedAt ?? visiblePoints.at(-1)?.observed_at ?? null);
  const xDomain = $derived.by(() => {
    const values = chartData.map((point) => point.observedAtMs);
    if (values.length === 0) return undefined;

    const min = Math.min(...values);
    const max = Math.max(...values);

    return min === max ? [min - 86_400_000, max + 86_400_000] : [min, max];
  });
  const yDomain = $derived.by(() => {
    const values = chartData.map((point) => point.value);
    if (values.length === 0) return undefined;

    const min = Math.min(...values);
    const max = Math.max(...values);

    return min === max ? [min - 1, max + 1] : undefined;
  });
  const stateTitle = $derived(visiblePoints.length === 0 ? 'Aggregate history unavailable' : 'Insufficient aggregate history');
  const stateMessage = $derived.by(() => {
    if (visiblePoints.length === 0) {
      return summary.no_data_reason ?? summary.current_only_reason ?? 'No real aggregate history points are available yet.';
    }

    if (visiblePoints.length >= 2 && chartData.length < 2) {
      return 'Aggregate points are available, but at least two dated points are required to draw a truthful time line.';
    }

    return summary.current_only_reason ?? 'Only one real aggregate point is available. A line chart needs at least two real points.';
  });

  function pointToDatum(point: PublicTrendAggregatePointRead): AggregateDatum | null {
    const observedAtMs = observedAtToTimestamp(point.observed_at);
    if (!Number.isFinite(observedAtMs)) return null;

    return {
      observedAtMs,
      observedAt: point.observed_at,
      value: point.value,
      metric: point.metric,
      label: point.label,
      memeCount: point.meme_count ?? 0,
      snapshotCount: point.snapshot_count ?? 0
    };
  }

  function observedAtToTimestamp(raw: string | null): number {
    if (!raw) return Number.NaN;

    const timestamp = Date.parse(raw);
    return Number.isFinite(timestamp) ? timestamp : Number.NaN;
  }

  function formatObservedAt(raw: string | null): string {
    if (!raw) return 'current window';

    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return raw;

    return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric' }).format(date);
  }

  function formatValue(value: number): string {
    return value.toFixed(1);
  }

  function formatCount(value: number | null | undefined): string {
    return numberFormatter.format(value ?? 0);
  }
</script>

{#snippet footer()}
  <span class="inline-flex items-center gap-2 rounded-full border border-line bg-paper px-3 py-2">
    <span class="h-2.5 w-2.5 rounded-full" style:background={lineColor}></span>
    {metricLabel}
  </span>
  <span class="rounded-full border border-line bg-paper px-3 py-2">
    {formatObservedAt(firstObservedAt)} to {formatObservedAt(lastObservedAt)}
  </span>
{/snippet}

<section class="grid gap-4" aria-label={`${summary.title} aggregate history`}>
  {#if canRenderLine}
    <ChartFrame
      label="Aggregate history"
      description="Real aggregate history points for this public tag or template. Exact values are listed in the table below."
      footer={footer}
    >
      <LayerChart
        data={chartData}
        x="observedAtMs"
        y="value"
        {xDomain}
        {yDomain}
        xPadding={[10, 10]}
        yPadding={[12, 12]}
        padding={{ top: 12, right: 18, bottom: 18, left: 48 }}
        tooltip={{ mode: 'quadtree' }}
        ssr
      >
        <LayerChartSvg title={`${summary.title} aggregate history line chart`}>
          <LayerChartGrid y={{ class: 'stroke-ink/10' }} yTicks={4} />
          <LayerChartAxis
            placement="left"
            rule={{ class: 'stroke-ink/20' }}
            ticks={4}
            classes={{ tick: 'stroke-ink/20', tickLabel: 'fill-muted text-[11px] font-bold' }}
          />
          <LayerChartSpline stroke={lineColor} strokeWidth={3} fill="none" stroke-linecap="round" stroke-linejoin="round" />
          <LayerChartPoints r={4} fill={lineColor} stroke="var(--color-paper)" strokeWidth={2} />
        </LayerChartSvg>

        <LayerChartTooltip.Root
          variant="none"
          classes={{ container: 'rounded-2xl border border-line bg-paper/95 px-3 py-2 text-sm text-ink shadow-warm-lg' }}
        >
          {#snippet children({ data }: { data: AggregateDatum })}
            <div class="grid gap-1">
              <p class="m-0 font-extrabold">{data.label}</p>
              <p class="m-0 text-muted">{formatObservedAt(data.observedAt)} · {data.metric}</p>
              <p class="m-0 font-extrabold tabular-nums">{formatValue(data.value)}</p>
              <p class="m-0 text-muted">{formatCount(data.memeCount)} memes · {formatCount(data.snapshotCount)} snapshots</p>
            </div>
          {/snippet}
        </LayerChartTooltip.Root>
      </LayerChart>
    </ChartFrame>
  {:else}
    <div class="rounded-[24px] border border-dashed border-line bg-soft p-5" role="status">
      <p class="m-0 font-extrabold text-ink">{stateTitle}</p>
      <p class="m-0 mt-2 text-sm text-muted">{stateMessage}</p>
    </div>
  {/if}

  {#if visiblePoints.length > 0}
    <div class="overflow-x-auto rounded-[24px] border border-line bg-paper">
      <table class="w-full min-w-[980px] border-collapse text-left text-sm">
        <caption class="sr-only">Exact aggregate history values for {summary.title}</caption>
        <thead>
          <tr class="border-b border-line text-muted">
            <th class="py-3 pl-4 pr-3" scope="col">Metric</th>
            <th class="py-3 pr-3" scope="col">Date/window</th>
            <th class="py-3 pr-3" scope="col">Value</th>
            <th class="py-3 pr-3" scope="col">Memes</th>
            <th class="py-3 pr-3" scope="col">Snapshots</th>
            <th class="py-3 pr-3" scope="col">Source views</th>
            <th class="py-3 pr-3" scope="col">Source reactions</th>
            <th class="py-3 pr-3" scope="col">Source reposts</th>
            <th class="py-3 pr-3" scope="col">Platform views</th>
            <th class="py-3 pr-3" scope="col">Sends</th>
            <th class="py-3 pr-3" scope="col">Saves</th>
            <th class="py-3 pr-4" scope="col">Likes</th>
          </tr>
        </thead>
        <tbody>
          {#each visiblePoints as point (`${point.observed_at ?? 'current'}:${point.metric}:${point.value}`)}
            <tr class="border-b border-line/70 align-top last:border-b-0">
              <th class="py-3 pl-4 pr-3 font-extrabold" scope="row">
                {point.label}
                <span class="block text-xs font-bold text-muted">{point.metric}</span>
              </th>
              <td class="py-3 pr-3">{formatObservedAt(point.observed_at)}</td>
              <td class="py-3 pr-3 tabular-nums">{formatValue(point.value)}</td>
              <td class="py-3 pr-3 tabular-nums">{formatCount(point.meme_count)}</td>
              <td class="py-3 pr-3 tabular-nums">{formatCount(point.snapshot_count)}</td>
              <td class="py-3 pr-3 tabular-nums">{formatCount(point.source_views)}</td>
              <td class="py-3 pr-3 tabular-nums">{formatCount(point.source_reactions)}</td>
              <td class="py-3 pr-3 tabular-nums">{formatCount(point.source_reposts)}</td>
              <td class="py-3 pr-3 tabular-nums">{formatCount(point.platform_views)}</td>
              <td class="py-3 pr-3 tabular-nums">{formatCount(point.platform_sends)}</td>
              <td class="py-3 pr-3 tabular-nums">{formatCount(point.platform_saves)}</td>
              <td class="py-3 pr-4 tabular-nums">{formatCount(point.platform_likes)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>
