<script lang="ts">
  import TrendComparisonChart from '$lib/features/trends/TrendComparisonChart.svelte';
  import type { PublicTrendComparisonSeriesRead } from '$lib/api/types';
  import { ActionLink, Card, EmptyState, Input, Notice, PageHeader } from '$lib/ui';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  const formItems = $derived([
    ...data.items,
    ...Array(Math.max(data.comparison.max_items - data.items.length, 0)).fill('')
  ].slice(0, data.comparison.max_items));
  const hasRequestedItems = $derived(data.items.length > 0);
  const limitedSeries = $derived(
    data.comparison.items.filter((item) => item.no_data_reason || item.current_only_reason || item.insufficient_history || item.points.length < 2)
  );

  function formatObservedAt(raw: string | null): string {
    if (!raw) return 'current window';
    return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric' }).format(new Date(raw));
  }

  function isAggregateSeries(item: PublicTrendComparisonSeriesRead): boolean {
    return item.kind === 'tag' || item.kind === 'template';
  }

  function seriesBasis(item: PublicTrendComparisonSeriesRead): string {
    if (item.kind === 'meme') return 'Per-meme snapshots';
    if (isAggregateSeries(item)) {
      return item.current_only_reason ? 'Current-window aggregate fallback' : 'Aggregate history points';
    }
    return 'Requested trend item';
  }

  function dataStatus(item: PublicTrendComparisonSeriesRead): string {
    if (item.no_data_reason) return item.no_data_reason;
    if (item.current_only_reason) return item.current_only_reason;
    if (item.insufficient_history) {
      if (item.kind === 'meme') return 'Insufficient history; at least two per-meme snapshots are needed.';
      if (isAggregateSeries(item) && item.points.length === 1) return 'Insufficient history; one real aggregate point is available.';
      if (isAggregateSeries(item)) return 'Insufficient aggregate history; no line is drawn.';
      return 'Insufficient history; no line is drawn.';
    }
    if (item.kind === 'meme') return 'Real per-meme snapshot history.';
    if (isAggregateSeries(item)) return 'Real aggregate history points.';
    return 'Real trend points.';
  }
</script>

<PageHeader
  title="Compare public trends."
  description="Share a URL with meme, tag, and template specs. Meme series use per-meme popularity snapshots. Tag and template series use aggregate history points when available, or an explicit current-window fallback when history is missing."
  badge="Shareable URL"
>
  <ActionLink href="/trends" variant="secondary">Back to trends</ActionLink>
  <ActionLink href="/trends/timeline" variant="secondary">Timeline</ActionLink>
</PageHeader>

{#if data.errorMessage}
  <Notice>{data.errorMessage}</Notice>
{/if}

<Card class="mb-6 grid gap-4 shadow-none">
  <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Choose items</h2>
  <p class="m-0 text-muted">Use specs like <code>meme:launch-reaction</code>, <code>tag:reaction</code>, or <code>template:frog-template</code>. Add up to {data.comparison.max_items} items.</p>
  <form class="grid gap-3" method="GET" action="/trends/compare">
    {#each formItems as item, index (`compare-input-${index}`)}
      <label class="grid gap-2 font-extrabold">
        <span>Item {index + 1}</span>
        <Input name="item" value={item} placeholder={index === 0 ? 'meme:uuid-or-slug' : index === 1 ? 'tag:reaction' : 'template:frog-template'} />
      </label>
    {/each}
    <div class="flex flex-wrap gap-2">
      <button class="rounded-[18px] bg-ink px-5 py-4 font-extrabold text-paper" type="submit">Compare</button>
      <ActionLink href="/trends/compare" variant="ghost">Clear</ActionLink>
    </div>
  </form>
</Card>

{#if !hasRequestedItems}
  <EmptyState title="Start with URL params" message="Enter items above or share a link like /trends/compare?item=tag:reaction&item=template:frog-template." />
{:else if data.comparison.items.length === 0 && !data.errorMessage}
  <EmptyState title="No comparison data" message="The API returned no requested items. Check the item specs and try again." />
{:else}
  <section class="grid gap-6" aria-label="Trend comparison results">
    <Card class="grid gap-4 shadow-none">
      <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Comparison chart</h2>
      <TrendComparisonChart series={data.comparison.items} />
      {#if limitedSeries.length > 0}
        <Notice>
          Some selected items are not drawn as full history lines. {limitedSeries.map((item) => `${item.title}: ${dataStatus(item)}`).join(' ')}
        </Notice>
      {/if}
    </Card>

    <Card class="overflow-x-auto shadow-none">
      <h2 class="m-0 mb-4 text-2xl font-black tracking-[-0.04em]">Data table</h2>
      <table class="w-full min-w-[720px] border-collapse text-left text-sm">
        <thead>
          <tr class="border-b border-line text-muted">
            <th class="py-3 pr-4">Item</th>
            <th class="py-3 pr-4">Kind</th>
            <th class="py-3 pr-4">Series basis</th>
            <th class="py-3 pr-4">Points</th>
            <th class="py-3 pr-4">Latest value</th>
            <th class="py-3 pr-4">Data status</th>
          </tr>
        </thead>
        <tbody>
          {#each data.comparison.items as item (`row-${item.kind}:${item.value}`)}
            {@const latestPoint = item.points.at(-1)}
            <tr class="border-b border-line/70 align-top">
              <td class="py-3 pr-4 font-extrabold">{item.title}</td>
              <td class="py-3 pr-4">{item.kind}</td>
              <td class="py-3 pr-4">{seriesBasis(item)}</td>
              <td class="py-3 pr-4">{item.points.length}</td>
              <td class="py-3 pr-4">
                {#if latestPoint}
                  {latestPoint.value.toFixed(1)} · {latestPoint.label} · {formatObservedAt(latestPoint.observed_at)}
                {:else}
                  No points
                {/if}
              </td>
              <td class="py-3 pr-4 text-muted">
                {dataStatus(item)}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </Card>
  </section>
{/if}
