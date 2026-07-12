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
    activity: number;
    sourceActivity: number;
    memeExpertActivity: number;
  };

  const numberFormatter = new Intl.NumberFormat('en');
  const lineColor = '#b45309';
  const recordedActivityDescription =
    'Recorded activity adds original-source views, reactions, and reposts to MemeExpert views, sends, saves, and favorites. It counts signals, not unique people.';
  const visiblePoints = $derived(summary.points ?? []);
  const chartData: AggregateDatum[] = $derived(
    visiblePoints
      .map((point) => pointToDatum(point))
      .filter((point): point is AggregateDatum => point !== null)
  );
  const canRenderLine = $derived(visiblePoints.length >= 2 && chartData.length >= 2);
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
    const values = chartData.map((point) => point.activity);
    if (values.length === 0) return undefined;

    const min = Math.min(...values);
    const max = Math.max(...values);

    return min === max ? [min - 1, max + 1] : undefined;
  });
  const stateTitle = $derived(visiblePoints.length === 0 ? 'Nothing to chart yet' : 'A new trend is taking shape');
  const stateMessage = $derived(
    visiblePoints.length === 0
      ? 'Activity will appear here as this collection of memes catches on.'
      : 'Come back soon to see how it changes.'
  );

  function pointToDatum(point: PublicTrendAggregatePointRead): AggregateDatum | null {
    const observedAtMs = observedAtToTimestamp(point.observed_at);
    if (!Number.isFinite(observedAtMs)) return null;

    return {
      observedAtMs,
      observedAt: point.observed_at,
      activity: recordedActivity(point),
      sourceActivity: sourceActivity(point),
      memeExpertActivity: memeExpertActivity(point)
    };
  }

  function observedAtToTimestamp(raw: string | null): number {
    if (!raw) return Number.NaN;

    const timestamp = Date.parse(raw);
    return Number.isFinite(timestamp) ? timestamp : Number.NaN;
  }

  function formatObservedAt(raw: string | null): string {
    if (!raw) return 'This week';

    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return 'This week';

    return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }).format(date);
  }

  function formatCount(value: number | null | undefined): string {
    return numberFormatter.format(count(value));
  }

  /**
   * Recorded activity is deliberately an unweighted count of all available
   * source and MemeExpert signals, rather than the API's popularity score.
   * The sources can overlap, so it is not a unique-person count.
   */
  function recordedActivity(point: PublicTrendAggregatePointRead): number {
    return sourceActivity(point) + memeExpertActivity(point);
  }

  function sourceActivity(point: PublicTrendAggregatePointRead): number {
    return count(point.source_views) + count(point.source_reactions) + count(point.source_reposts);
  }

  function memeExpertActivity(point: PublicTrendAggregatePointRead): number {
    return count(point.platform_views) + count(point.platform_sends) + count(point.platform_saves) + count(point.platform_likes);
  }

  function count(value: number | null | undefined): number {
    return typeof value === 'number' && Number.isFinite(value) ? Math.max(value, 0) : 0;
  }
</script>

{#snippet footer()}
  <span class="inline-flex items-center gap-2 rounded-full border border-line bg-paper px-3 py-2">
    <span class="h-2.5 w-2.5 rounded-full" style:background={lineColor}></span>
    Recorded activity
  </span>
  <span class="rounded-full border border-line bg-paper px-3 py-2">
    {formatObservedAt(firstObservedAt)} to {formatObservedAt(lastObservedAt)}
  </span>
{/snippet}

<section class="grid gap-4" aria-label={`${summary.title} recorded activity over time`}>
  {#if canRenderLine}
    <ChartFrame
      label="Recorded activity over time"
      description={`${recordedActivityDescription} A readable table follows.`}
      footer={footer}
    >
      <p class="sr-only">The vertical axis shows recorded activity signals. The horizontal direction moves from earlier to later activity.</p>
      <LayerChart
        data={chartData}
        x="observedAtMs"
        y="activity"
        {xDomain}
        {yDomain}
        xPadding={[10, 10]}
        yPadding={[12, 12]}
        padding={{ top: 12, right: 18, bottom: 18, left: 48 }}
        tooltip={{ mode: 'quadtree' }}
        ssr
      >
        <LayerChartSvg title={`${summary.title} recorded activity over time`}>
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
              <p class="m-0 font-extrabold">{formatObservedAt(data.observedAt)}</p>
              <p class="m-0 font-extrabold">Recorded activity: {formatCount(data.activity)} signals</p>
              <p class="m-0 text-muted">Original sources: {formatCount(data.sourceActivity)} · MemeExpert: {formatCount(data.memeExpertActivity)}</p>
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
    <div class="overflow-x-auto rounded-xl border border-line bg-paper">
      <table class="w-full min-w-[680px] border-collapse text-left text-sm">
        <caption class="sr-only">Recorded activity details for {summary.title}</caption>
        <thead>
          <tr class="border-b border-line text-muted">
            <th class="py-3 pl-4 pr-3" scope="col">Date</th>
            <th class="py-3 pr-3" scope="col">Recorded activity</th>
            <th class="py-3 pr-3" scope="col">Original sources</th>
            <th class="py-3 pr-3" scope="col">MemeExpert</th>
            <th class="py-3 pr-4" scope="col">Memes</th>
          </tr>
        </thead>
        <tbody>
          {#each visiblePoints as point, index (`${point.observed_at ?? 'current'}:${recordedActivity(point)}:${index}`)}
            <tr class="border-b border-line/70 align-top last:border-b-0">
              <th class="py-3 pl-4 pr-3 font-extrabold" scope="row">{formatObservedAt(point.observed_at)}</th>
              <td class="py-3 pr-3 tabular-nums">{formatCount(recordedActivity(point))}</td>
              <td class="py-3 pr-3 tabular-nums">{formatCount(sourceActivity(point))}</td>
              <td class="py-3 pr-3 tabular-nums">{formatCount(memeExpertActivity(point))}</td>
              <td class="py-3 pr-4 tabular-nums">{formatCount(point.meme_count)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>
