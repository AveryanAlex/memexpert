<script lang="ts">
  import type { PublicMemeAnalyticsRead, PublicMemeObservedSourcePointRead } from '$lib/api/types';
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
  import {
    canPlotMemeActivity,
    knownObservedTelegramPoints,
    memeActivityChartDatum,
    observedTelegramLineData,
    type MemeActivityChartDatum,
    type ObservedTelegramMetric
  } from './meme-activity-chart';

  let { analytics }: { analytics: PublicMemeAnalyticsRead } = $props();

  const numberFormatter = new Intl.NumberFormat('en');
  const rateFormatter = new Intl.NumberFormat('en', { maximumFractionDigits: 2 });
  const activityColor = '#b45309';
  const sourceActivityColor = '#201a14';
  const memeExpertActivityColor = '#047857';
  const observedMetrics: ReadonlyArray<{ key: ObservedTelegramMetric; label: string; color: string }> = [
    { key: 'views', label: 'Views', color: chartSeriesPalette[0] },
    { key: 'reactions', label: 'Reactions', color: chartSeriesPalette[1] },
    { key: 'comments', label: 'Comments', color: chartSeriesPalette[5] },
    { key: 'reposts', label: 'Reposts', color: chartSeriesPalette[2] }
  ];

  let selectedObservedMetric = $state<ObservedTelegramMetric>('views');

  const activityData: MemeActivityChartDatum[] = $derived(
    analytics.activity_points
      .map(memeActivityChartDatum)
      .filter((point): point is MemeActivityChartDatum => point !== null)
  );
  const canPlotActivity = $derived(canPlotMemeActivity(activityData));
  const sourceObservations: PublicMemeObservedSourcePointRead[] = $derived([
    analytics.observed_source.opening_baseline,
    ...analytics.observed_source.points
  ]);
  const selectedObservedConfig = $derived(
    observedMetrics.find((metric) => metric.key === selectedObservedMetric) ?? observedMetrics[0]
  );
  const observedLineData = $derived(observedTelegramLineData(sourceObservations, selectedObservedMetric));
  const observedKnownData = $derived(knownObservedTelegramPoints(observedLineData));
  const canPlotObserved = $derived(observedKnownData.length >= 2);
  const activityDomain = $derived(timeDomain(activityData.map((point) => point.observedMs)));
  const observedDomain = $derived(timeDomain(observedLineData.map((point) => point.observedMs)));
  const activityYDomain = $derived(valueDomain(activityData.map((point) => point.activityPerDay)));
  const observedYDomain = $derived(valueDomain(observedKnownData.map((point) => point.value)));

  function timeDomain(values: number[]): number[] | undefined {
    if (values.length === 0) return undefined;
    const min = Math.min(...values);
    const max = Math.max(...values);
    return min === max ? [min - 86_400_000, max + 86_400_000] : [min, max];
  }

  function valueDomain(values: number[]): number[] {
    const maximum = Math.max(0, ...values);
    return [0, maximum > 0 ? maximum * 1.05 : 1];
  }

  function formatDate(raw: string): string {
    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return 'Unknown date';
    return new Intl.DateTimeFormat('en', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      timeZone: 'UTC'
    }).format(date);
  }

  function formatDateTime(raw: string): string {
    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return 'Unknown date';
    return `${new Intl.DateTimeFormat('en', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
      timeZone: 'UTC'
    }).format(date)} UTC`;
  }

  function formatAxisDate(value: unknown): string {
    const timestamp = typeof value === 'number' ? value : Number(value);
    if (!Number.isFinite(timestamp)) return '';
    return new Intl.DateTimeFormat('en', {
      month: 'short',
      day: 'numeric',
      year: '2-digit',
      timeZone: 'UTC'
    }).format(new Date(timestamp));
  }

  function formatCount(value: number): string {
    return numberFormatter.format(value);
  }

  function formatRate(value: number): string {
    return rateFormatter.format(value);
  }

  function formatGranularity(value: MemeActivityChartDatum['granularity']): string {
    return `${value.charAt(0).toUpperCase()}${value.slice(1)}`;
  }

  function durationLabel(value: number): string {
    return `${formatRate(value)} ${value === 1 ? 'day' : 'days'}`;
  }
</script>

{#snippet activityFooter()}
  <span class="inline-flex items-center gap-2 rounded-full border border-line bg-paper px-3 py-2">
    <span class="size-2.5 rounded-full" style:background={activityColor}></span>
    Total signals/day
  </span>
  <span class="inline-flex items-center gap-2 rounded-full border border-line bg-paper px-3 py-2">
    <span class="size-2.5 rounded-full" style:background={sourceActivityColor}></span>
    Telegram signals/day
  </span>
  <span class="inline-flex items-center gap-2 rounded-full border border-line bg-paper px-3 py-2">
    <span class="size-2.5 rounded-full" style:background={memeExpertActivityColor}></span>
    MemeExpert signals/day
  </span>
  <span class="rounded-full border border-line bg-paper px-3 py-2">Raw totals remain in the table</span>
{/snippet}

{#snippet observedFooter()}
  <span class="inline-flex items-center gap-2 rounded-full border border-line bg-paper px-3 py-2">
    <span class="size-2.5 rounded-full" style:background={selectedObservedConfig.color}></span>
    {selectedObservedConfig.label}
  </span>
  <span class="rounded-full border border-line bg-paper px-3 py-2">End state at the last capture in each server bucket</span>
{/snippet}

<div class="grid gap-6 xl:grid-cols-2">
  <ChartFrame
    label="Recorded activity · signals per day"
    description="Each day, week, or month bucket is divided by its duration so adaptive all-time history remains comparable. Downloads stay excluded; exact bucket totals follow below."
    empty={!canPlotActivity}
    emptyTitle="Not enough activity history yet"
    emptyMessage="At least two buckets, including one with Recorded activity, are needed for a trend line. Exact zero and raw-total buckets remain in the data table."
    footer={canPlotActivity ? activityFooter : undefined}
  >
    <LayerChart
      data={activityData}
      x="observedMs"
      y="activityPerDay"
      xDomain={activityDomain}
      yDomain={activityYDomain}
      xPadding={[10, 10]}
      yPadding={[12, 12]}
      padding={{ top: 12, right: 18, bottom: 48, left: 52 }}
      tooltip={{ mode: 'quadtree' }}
      ssr
    >
      <LayerChartSvg title="Recorded activity signals per day over time">
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
          ticks={3}
          format={formatAxisDate}
          classes={{ tick: 'stroke-ink/20', tickLabel: 'fill-muted text-[10px] font-bold' }}
        />
        <LayerChartSpline
          data={activityData}
          x="observedMs"
          y="sourcesPerDay"
          stroke={sourceActivityColor}
          strokeWidth={2}
          fill="none"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
        <LayerChartSpline
          data={activityData}
          x="observedMs"
          y="memeExpertPerDay"
          stroke={memeExpertActivityColor}
          strokeWidth={2}
          fill="none"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
        <LayerChartSpline stroke={activityColor} strokeWidth={3} fill="none" stroke-linecap="round" stroke-linejoin="round" />
        <LayerChartPoints r={4} fill={activityColor} stroke="var(--color-paper)" strokeWidth={2} />
      </LayerChartSvg>
      <LayerChartTooltip.Root
        variant="none"
        classes={{ container: 'rounded-2xl border border-line bg-paper/95 px-3 py-2 text-sm text-ink shadow-warm-lg' }}
      >
        {#snippet children({ data: point }: { data: MemeActivityChartDatum })}
          <div class="grid gap-1">
            <p class="m-0 font-extrabold">{formatDate(point.bucketStart)} to {formatDate(point.bucketEnd)}</p>
            <p class="m-0 text-muted">{formatGranularity(point.granularity)} bucket · {durationLabel(point.durationDays)}</p>
            <p class="m-0 font-extrabold">{formatRate(point.activityPerDay)} signals/day</p>
            <p class="m-0 text-muted">Raw total {formatCount(point.rawActivity)} · Sources {formatCount(point.rawSources)} · MemeExpert {formatCount(point.rawMemeExpert)}</p>
          </div>
        {/snippet}
      </LayerChartTooltip.Root>
    </LayerChart>
  </ChartFrame>

  <div class="grid content-start gap-3">
    <fieldset class="m-0 flex flex-wrap gap-2 border-0 p-0" aria-label="Telegram counter shown in chart">
      <legend class="mb-2 w-full text-sm font-extrabold">Telegram counter</legend>
      {#each observedMetrics as metric (metric.key)}
        <label class="inline-flex cursor-pointer items-center gap-2 rounded-full border border-line bg-paper px-3 py-2 text-sm font-semibold">
          <input type="radio" name="telegram-counter" value={metric.key} bind:group={selectedObservedMetric} />
          {metric.label}
        </label>
      {/each}
    </fieldset>

    <ChartFrame
      label={`Observed Telegram ${selectedObservedConfig.label.toLowerCase()}`}
      description="Absolute server-bucketed end states, stamped at the last real capture in each bucket; the opening baseline is as of the range start. Missing values break the line."
      empty={!canPlotObserved}
      emptyTitle={`Not enough known ${selectedObservedConfig.label.toLowerCase()} yet`}
      emptyMessage="At least two known bucket-end values are needed for a line. Switch counters or return after more observations."
      footer={canPlotObserved ? observedFooter : undefined}
    >
      <LayerChart
        data={observedKnownData}
        x="observedMs"
        y="value"
        xDomain={observedDomain}
        yDomain={observedYDomain}
        xPadding={[10, 10]}
        yPadding={[12, 12]}
        padding={{ top: 12, right: 18, bottom: 48, left: 52 }}
        tooltip={{ mode: 'quadtree' }}
        ssr
      >
        <LayerChartSvg title={`Observed cumulative Telegram ${selectedObservedConfig.label.toLowerCase()} over time`}>
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
            ticks={3}
            format={formatAxisDate}
            classes={{ tick: 'stroke-ink/20', tickLabel: 'fill-muted text-[10px] font-bold' }}
          />
          <LayerChartSpline
            data={observedLineData}
            x="observedMs"
            y="value"
            stroke={selectedObservedConfig.color}
            strokeWidth={3}
            fill="none"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
          <LayerChartPoints
            data={observedKnownData}
            x="observedMs"
            y="value"
            r={4}
            fill={selectedObservedConfig.color}
            stroke="var(--color-paper)"
            strokeWidth={2}
          />
        </LayerChartSvg>
        <LayerChartTooltip.Root
          variant="none"
          classes={{ container: 'rounded-2xl border border-line bg-paper/95 px-3 py-2 text-sm text-ink shadow-warm-lg' }}
        >
          {#snippet children({ data: point }: { data: (typeof observedKnownData)[number] })}
            <div class="grid gap-1">
              <p class="m-0 font-extrabold">{selectedObservedConfig.label} · {formatCount(point.value)}</p>
              <p class="m-0 text-muted">Observed / as of {formatDateTime(point.observedAt)}</p>
            </div>
          {/snippet}
        </LayerChartTooltip.Root>
      </LayerChart>
    </ChartFrame>
  </div>
</div>

{#if activityData.length > 0 || sourceObservations.length > 0}
  <details class="rounded-xl border border-line bg-soft/60">
    <summary class="cursor-pointer px-4 py-3 text-sm font-extrabold">View chart data</summary>
    <div class="grid gap-5 border-t border-line p-4">
      <div class="overflow-x-auto">
        <table class="w-full min-w-[900px] border-collapse text-left text-sm">
          <caption class="mb-2 text-left font-extrabold">Exact Recorded activity buckets</caption>
          <thead>
            <tr class="border-b border-line text-muted">
              <th class="py-2 pr-3">Bucket start</th><th class="py-2 pr-3">Bucket end</th><th class="py-2 pr-3">Granularity</th><th class="py-2 pr-3">Duration</th><th class="py-2 pr-3">Recorded total</th><th class="py-2 pr-3">Source total</th><th class="py-2 pr-3">MemeExpert total</th><th class="py-2">Signals/day</th>
            </tr>
          </thead>
          <tbody>
            {#each activityData as point (`${point.bucketStart}:${point.bucketEnd}`)}
              <tr class="border-b border-line/60 last:border-0">
                <th class="py-2 pr-3 font-semibold"><time datetime={point.bucketStart}>{formatDateTime(point.bucketStart)}</time></th>
                <td class="py-2 pr-3"><time datetime={point.bucketEnd}>{formatDateTime(point.bucketEnd)}</time></td>
                <td class="py-2 pr-3">{formatGranularity(point.granularity)}</td>
                <td class="py-2 pr-3 tabular-nums">{durationLabel(point.durationDays)}</td>
                <td class="py-2 pr-3 tabular-nums">{formatCount(point.rawActivity)}</td>
                <td class="py-2 pr-3 tabular-nums">{formatCount(point.rawSources)}</td>
                <td class="py-2 pr-3 tabular-nums">{formatCount(point.rawMemeExpert)}</td>
                <td class="py-2 tabular-nums">{formatRate(point.activityPerDay)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full min-w-[620px] border-collapse text-left text-sm">
          <caption class="mb-2 text-left font-extrabold">Server-bucketed Telegram end states at the last capture in each bucket</caption>
          <thead><tr class="border-b border-line text-muted"><th class="py-2 pr-3">Observed / as of</th><th class="py-2 pr-3">Views</th><th class="py-2 pr-3">Reactions</th><th class="py-2 pr-3">Comments</th><th class="py-2">Reposts</th></tr></thead>
          <tbody>
            {#each sourceObservations as point, index (`${point.observed_at}:${index}`)}
              <tr class="border-b border-line/60 last:border-0"><th class="py-2 pr-3 font-semibold"><time datetime={point.observed_at}>{formatDateTime(point.observed_at)}</time></th><td class="py-2 pr-3 tabular-nums">{point.views === null ? 'Unknown' : formatCount(point.views)}</td><td class="py-2 pr-3 tabular-nums">{point.reactions === null ? 'Unknown' : formatCount(point.reactions)}</td><td class="py-2 pr-3 tabular-nums">{point.comments === null ? 'Unknown' : formatCount(point.comments)}</td><td class="py-2 tabular-nums">{point.reposts === null ? 'Unknown' : formatCount(point.reposts)}</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  </details>
{/if}
