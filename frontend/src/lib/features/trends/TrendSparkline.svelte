<script lang="ts">
  import type { PublicMemePopularityPointRead } from '$lib/api/types';
  import { ChartFrame, LayerChart, LayerChartSpline, LayerChartSvg } from '$lib/ui/chart';

  let { points }: { points: PublicMemePopularityPointRead[] } = $props();

  type SparklineDatum = {
    index: number;
    popularityScore: number;
  };

  const chartData: SparklineDatum[] = $derived(
    points.map((point, index) => ({
      index,
      popularityScore: point.popularity_score
    }))
  );

  const yDomain = $derived.by(() => {
    const scores = chartData.map((point) => point.popularityScore);
    const min = Math.min(...scores);
    const max = Math.max(...scores);

    return min === max ? [min - 1, max + 1] : undefined;
  });
</script>

{#if points.length >= 2}
  <ChartFrame
    label="Popularity sparkline"
    showCaption={false}
    size="compact"
    class="max-w-[420px]"
    plotClass="h-24 !min-h-24 rounded-[18px] p-2.5 sm:!min-h-24"
  >
    <LayerChart
      data={chartData}
      x="index"
      y="popularityScore"
      {yDomain}
      xPadding={[8, 8]}
      yPadding={[8, 8]}
      ssr
      pointerEvents={false}
    >
      <LayerChartSvg title="Popularity sparkline" pointerEvents={false}>
        <LayerChartSpline stroke="currentColor" strokeWidth={3} stroke-linecap="round" stroke-linejoin="round" />
      </LayerChartSvg>
    </LayerChart>
  </ChartFrame>
{:else}
  <p class="m-0 text-muted">Not enough real snapshots for a sparkline yet.</p>
{/if}
