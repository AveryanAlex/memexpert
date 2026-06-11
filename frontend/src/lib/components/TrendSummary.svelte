<script lang="ts">
  import type { PublicTrendMetricsRead } from '$lib/api/types';

  let { trend }: { trend: PublicTrendMetricsRead | null } = $props();
</script>

{#if trend}
  <div class="trend-stats" aria-label="Trend summary">
    <span>{trend.recent.views} views</span>
    <span>{trend.recent.sends} sends</span>
    <span>{trend.recent.likes} likes</span>
    <span>{trend.engagement_24h.toFixed(0)} engagement 24h</span>
    <span>score {trend.trending_score.toFixed(1)}</span>
  </div>
{:else}
  <p class="muted">Trend projections have no data for this item yet.</p>
{/if}
