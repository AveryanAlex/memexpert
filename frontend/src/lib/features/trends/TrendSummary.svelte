<script lang="ts">
  import type { PublicTrendCountsRead, PublicTrendMetricsRead } from '$lib/api/types';
  import Badge from '$lib/ui/Badge.svelte';

  let { trend }: { trend: PublicTrendMetricsRead | null } = $props();

  const numberFormatter = new Intl.NumberFormat('en');
  const recentTotal = $derived(trend ? totalActivity(trend.recent) : 0);
  const previousTotal = $derived(trend ? totalActivity(trend.previous) : 0);
  const direction = $derived(
    recentTotal > previousTotal ? 'Rising' : previousTotal > recentTotal ? 'Slowing' : recentTotal > 0 ? 'Steady' : 'New'
  );
  const thisWeek = $derived(trend ? formatWeeklyActivity(trend.recent) : '');
  const change = $derived(formatChange(recentTotal - previousTotal));
  const latestRecordedActivity = $derived(trend ? recordedActivity(trend) : 0);

  function totalActivity(counts: PublicTrendCountsRead): number {
    return counts.views + counts.sends + counts.likes + counts.saves + counts.downloads;
  }

  function formatWeeklyActivity(counts: PublicTrendCountsRead): string {
    const entries = [
      counts.views > 0 ? `${formatCount(counts.views)} ${pluralize(counts.views, 'view')}` : null,
      counts.sends > 0 ? `${formatCount(counts.sends)} ${pluralize(counts.sends, 'send')}` : null,
      counts.likes > 0 ? `${formatCount(counts.likes)} ${pluralize(counts.likes, 'favorite')}` : null,
      counts.saves > 0 ? `${formatCount(counts.saves)} ${pluralize(counts.saves, 'save')}` : null
    ].filter((entry): entry is string => Boolean(entry));

    return entries.length > 0 ? `This week: ${entries.slice(0, 3).join(' · ')}` : 'This week: just getting started';
  }

  function formatChange(delta: number): string | null {
    if (delta > 0) return `${formatCount(delta)} more ${pluralize(delta, 'interaction')} than last week`;
    if (delta < 0) return `${formatCount(Math.abs(delta))} fewer ${pluralize(Math.abs(delta), 'interaction')} than last week`;
    return null;
  }

  /**
   * Keep the public label aligned with chart and timeline totals: source views,
   * reactions, and reposts plus MemeExpert views, sends, saves, and favorites.
   */
  function recordedActivity(metrics: PublicTrendMetricsRead): number {
    return (
      count(metrics.latest_source_views) +
      count(metrics.latest_source_reactions) +
      count(metrics.latest_source_reposts) +
      count(metrics.latest_platform_views) +
      count(metrics.latest_platform_sends) +
      count(metrics.latest_platform_saves) +
      count(metrics.latest_platform_likes)
    );
  }

  function formatCount(value: number): string {
    return numberFormatter.format(value);
  }

  function count(value: number | null | undefined): number {
    return typeof value === 'number' && Number.isFinite(value) ? Math.max(value, 0) : 0;
  }

  function pluralize(value: number, label: string): string {
    return value === 1 ? label : `${label}s`;
  }
</script>

{#if trend}
  <div class="grid gap-2" aria-label={`${direction}. ${thisWeek}. Latest recorded activity: ${latestRecordedActivity} signals${change ? `. ${change}` : ''}`}>
    <div class="flex flex-wrap items-center gap-2">
      <Badge tone="trend">{direction}</Badge>
      <p class="m-0 text-sm font-semibold text-ink">{thisWeek}</p>
    </div>
    <p class="m-0 text-sm text-muted">Latest recorded activity: {formatCount(latestRecordedActivity)} signals</p>
    {#if change}
      <p class="m-0 text-sm text-muted">{change}</p>
    {/if}
  </div>
{:else}
  <p class="m-0 text-muted">This meme is just getting started.</p>
{/if}
