<script lang="ts">
  import type { AdminAnalyticsBreakdownRead, AdminAnalyticsSurfaceRead } from '$lib/api/types';
  import { ChartFrame, LayerChartPieChart, chartSeriesPalette } from '$lib/ui/chart';
  import { aggregateAnalyticsCategories, formatAnalyticsNumber } from './format';

  type DonutDatum = { label: string; count: number; color: string };

  let { items, label = 'Product surfaces' }: { items: Array<AdminAnalyticsBreakdownRead | AdminAnalyticsSurfaceRead>; label?: string } = $props();

  const categories = $derived(aggregateAnalyticsCategories(items.filter((item) => item.count > 0), 6));
  const data: DonutDatum[] = $derived(
    categories.items.map((item, index) => ({ ...item, color: chartSeriesPalette[index % chartSeriesPalette.length] }))
  );
  const total = $derived(data.reduce((sum, item) => sum + item.count, 0));
</script>

<section class="grid gap-4" aria-label={label}>
  <ChartFrame
    {label}
    description="Event counts by the product surface where they were recorded."
    empty={data.length === 0}
    emptyTitle="No surface activity yet"
    emptyMessage="Surface mix becomes available when first-party product events are recorded."
    size="compact"
    plotClass="min-h-56 sm:min-h-56"
  >
    {#if data.length > 0}
      <p class="sr-only">A donut chart showing product surfaces. Exact counts are listed below.</p>
      <LayerChartPieChart
        {data}
        key="label"
        label="label"
        value="count"
        c="color"
        innerRadius={0.64}
        cornerRadius={3}
        padAngle={0.02}
        tooltip={{ mode: 'bounds' }}
        ssr
        props={{ svg: { title: label } }}
      />
    {/if}
  </ChartFrame>

  {#if data.length > 0}
    <ul class="m-0 grid list-none gap-2 p-0" aria-label={`${label} exact values`}>
      {#each data as item (item.label)}
        <li class="flex items-center justify-between gap-3 rounded-xl border border-line bg-paper px-3 py-2 text-sm">
          <span class="flex items-center gap-2 font-extrabold"><span class="size-2.5 rounded-full" style:background={item.color}></span>{item.label}</span>
          <span class="tabular-nums text-muted">{formatAnalyticsNumber(item.count)} · {total ? Math.round((item.count / total) * 100) : 0}%</span>
        </li>
      {/each}
    </ul>
    {#if categories.aggregated}
      <p class="m-0 text-sm text-muted">Smaller categories are combined into Other; percentages use the full total.</p>
    {/if}
  {/if}
</section>
