<script lang="ts">
  import type { PublicMemePopularityPointRead } from '$lib/api/types';

  let { points }: { points: PublicMemePopularityPointRead[] } = $props();

  const values = $derived(points.map((point) => point.popularity_score));
  const min = $derived(values.length ? Math.min(...values) : 0);
  const max = $derived(values.length ? Math.max(...values) : 0);
  const polyline = $derived(
    values
      .map((value, index) => {
        const x = values.length <= 1 ? 50 : (index / (values.length - 1)) * 100;
        const ratio = max === min ? 0.5 : (value - min) / (max - min);
        return `${x.toFixed(2)},${(42 - ratio * 34).toFixed(2)}`;
      })
      .join(' ')
  );
</script>

{#if points.length >= 2}
  <svg class="h-24 w-full max-w-[420px] rounded-[18px] bg-gradient-to-b from-paper to-soft p-2.5 text-ink" viewBox="0 0 100 48" role="img" aria-label="Popularity sparkline">
    <polyline points={polyline} fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />
  </svg>
{:else}
  <p class="m-0 text-muted">Not enough real snapshots for a sparkline yet.</p>
{/if}
