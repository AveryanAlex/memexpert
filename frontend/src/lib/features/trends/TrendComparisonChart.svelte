<script lang="ts">
  import type { PublicTrendComparisonSeriesRead } from '$lib/api/types';

  let { series }: { series: PublicTrendComparisonSeriesRead[] } = $props();

  const plottedSeries = $derived(series.filter((item) => item.points.length > 0));
  const values = $derived(plottedSeries.flatMap((item) => item.points.map((point) => point.value)));
  const min = $derived(values.length ? Math.min(...values) : 0);
  const max = $derived(values.length ? Math.max(...values) : 0);
  const colors = ['#201a14', '#b45309', '#047857', '#7c3aed', '#be123c', '#0369a1'];

  function pointPosition(item: PublicTrendComparisonSeriesRead, index: number): { x: number; y: number } {
    const point = item.points[index];
    const x = item.points.length <= 1 ? 50 : 8 + (index / (item.points.length - 1)) * 84;
    const ratio = max === min ? 0.5 : (point.value - min) / (max - min);
    return { x, y: 84 - ratio * 68 };
  }

  function pathFor(item: PublicTrendComparisonSeriesRead): string {
    return item.points
      .map((_, index) => {
        const point = pointPosition(item, index);
        return `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`;
      })
      .join(' ');
  }
</script>

{#if plottedSeries.length > 0}
  <figure class="m-0 grid gap-4">
    <svg class="h-72 w-full rounded-[24px] border border-line bg-gradient-to-b from-paper to-soft p-3 text-ink" viewBox="0 0 100 100" role="img" aria-label="Trend comparison line chart">
      <line x1="8" y1="84" x2="96" y2="84" stroke="currentColor" stroke-opacity="0.18" stroke-width="1" />
      <line x1="8" y1="16" x2="8" y2="84" stroke="currentColor" stroke-opacity="0.18" stroke-width="1" />
      {#each plottedSeries as item, seriesIndex (`${item.kind}:${item.value}`)}
        <path d={pathFor(item)} fill="none" stroke={colors[seriesIndex % colors.length]} stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round" />
        {#each item.points as _, pointIndex}
          {@const point = pointPosition(item, pointIndex)}
          <circle cx={point.x} cy={point.y} r="2.2" fill={colors[seriesIndex % colors.length]}>
            <title>{item.title}: {item.points[pointIndex].value.toFixed(1)}</title>
          </circle>
        {/each}
      {/each}
    </svg>
    <figcaption class="flex flex-wrap gap-2 text-sm text-muted">
      {#each plottedSeries as item, seriesIndex (`legend:${item.kind}:${item.value}`)}
        <span class="inline-flex items-center gap-2 rounded-full border border-line bg-paper px-3 py-2">
          <span class="h-2.5 w-2.5 rounded-full" style={`background:${colors[seriesIndex % colors.length]}`}></span>
          {item.title}
        </span>
      {/each}
    </figcaption>
  </figure>
{:else}
  <p class="m-0 text-muted">No real comparison points are available for the selected items yet.</p>
{/if}
