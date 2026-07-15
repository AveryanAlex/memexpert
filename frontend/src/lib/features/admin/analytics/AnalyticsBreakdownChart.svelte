<script lang="ts">
  import type { AdminAnalyticsBreakdownRead } from '$lib/api/types';
  import { ChartFrame, LayerChartBarChart } from '$lib/ui/chart';
  import { aggregateAnalyticsCategories, formatAnalyticsNumber } from './format';

  type BreakdownDatum = { label: string; count: number };

  let {
    label,
    description,
    items,
    emptyTitle = 'No breakdown data yet',
    emptyMessage = 'This distribution will appear as activity is recorded.',
    limit = 8
  }: {
    label: string;
    description: string;
    items: AdminAnalyticsBreakdownRead[];
    emptyTitle?: string;
    emptyMessage?: string;
    limit?: number;
  } = $props();

  const categories = $derived(aggregateAnalyticsCategories(items, limit));
  const data: BreakdownDatum[] = $derived(categories.items);
  const hasData = $derived(data.some((item) => item.count > 0));
</script>

<section class="grid gap-4" aria-label={label}>
  <ChartFrame
    {label}
    {description}
    empty={!hasData}
    {emptyTitle}
    {emptyMessage}
    size="tall"
    plotClass="min-h-[20rem]"
  >
    {#if hasData}
      <p class="sr-only">A horizontal bar chart comparing counts by category. The exact values appear in the table below.</p>
      <LayerChartBarChart
        {data}
        x="count"
        y="label"
        orientation="horizontal"
        bandPadding={0.35}
        tooltip={{ mode: 'bounds' }}
        ssr
        props={{
          bars: { fill: '#b45309', stroke: 'var(--color-paper)' },
          xAxis: { classes: { tick: 'stroke-ink/20', tickLabel: 'fill-muted text-[11px] font-bold' } },
          yAxis: { classes: { tick: 'stroke-ink/20', tickLabel: 'fill-ink text-[11px] font-bold' } },
          grid: { x: { class: 'stroke-ink/10' } },
          svg: { title: label }
        }}
      />
    {/if}
  </ChartFrame>

  {#if data.length > 0}
    <div class="overflow-x-auto rounded-2xl border border-line bg-paper">
      <table class="w-full border-collapse text-left text-sm">
        <caption class="sr-only">Exact values for {label.toLowerCase()}.</caption>
        <thead class="bg-soft text-muted">
          <tr>
            <th class="px-4 py-3 font-black" scope="col">Category</th>
            <th class="px-4 py-3 text-right font-black" scope="col">Count</th>
          </tr>
        </thead>
        <tbody>
          {#each data as item (item.label)}
            <tr class="border-t border-line">
              <th class="px-4 py-3 font-extrabold" scope="row">{item.label}</th>
              <td class="px-4 py-3 text-right tabular-nums">{formatAnalyticsNumber(item.count)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if categories.aggregated}
      <p class="m-0 text-sm text-muted">Smaller categories are combined into Other.</p>
    {/if}
  {/if}
</section>
