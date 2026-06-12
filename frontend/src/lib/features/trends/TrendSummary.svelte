<script lang="ts">
  import type { PublicTrendMetricsRead } from '$lib/api/types';
  import Badge from '$lib/ui/Badge.svelte';

  let { trend }: { trend: PublicTrendMetricsRead | null } = $props();
</script>

{#if trend}
  <div class="flex flex-wrap gap-2" aria-label="Trend summary">
    <Badge tone="trend">{trend.recent.views} views</Badge>
    <Badge tone="trend">{trend.recent.sends} sends</Badge>
    <Badge tone="trend">{trend.recent.likes} likes</Badge>
    <Badge tone="trend">{trend.engagement_24h.toFixed(0)} engagement 24h</Badge>
    <Badge tone="trend">score {trend.trending_score.toFixed(1)}</Badge>
  </div>
{:else}
  <p class="m-0 text-muted">Trend projections have no data for this item yet.</p>
{/if}
