<script lang="ts">
  import TrendComparisonChart from '$lib/features/trends/TrendComparisonChart.svelte';
  import { ActionLink, Card, EmptyState, Input, Notice, PageHeader } from '$lib/ui';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  const formItems = $derived([
    ...data.items,
    ...Array(Math.max(data.comparison.max_items - data.items.length, 0)).fill('')
  ].slice(0, data.comparison.max_items));
  const hasRequestedItems = $derived(data.items.length > 0);
  const seriesWithData = $derived(data.comparison.items.filter((item) => item.points.length > 0));

  function formatObservedAt(raw: string | null): string {
    if (!raw) return 'current window';
    return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric' }).format(new Date(raw));
  }
</script>

<PageHeader
  title="Compare public trends."
  description="Share a URL with meme, tag, and template specs. Meme lines use real captured popularity snapshots; tag and template aggregates show only the current materialized trend window."
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
      {#if seriesWithData.some((item) => item.insufficient_history)}
        <Notice>Some items only have a current aggregate point. Tags and templates do not have historical snapshot series yet, so they are not comparable as full history lines.</Notice>
      {/if}
    </Card>

    <Card class="overflow-x-auto shadow-none">
      <h2 class="m-0 mb-4 text-2xl font-black tracking-[-0.04em]">Data table</h2>
      <table class="w-full min-w-[720px] border-collapse text-left text-sm">
        <thead>
          <tr class="border-b border-line text-muted">
            <th class="py-3 pr-4">Item</th>
            <th class="py-3 pr-4">Kind</th>
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
              <td class="py-3 pr-4">{item.points.length}</td>
              <td class="py-3 pr-4">
                {#if latestPoint}
                  {latestPoint.value.toFixed(1)} · {formatObservedAt(latestPoint.observed_at)}
                {:else}
                  No points
                {/if}
              </td>
              <td class="py-3 pr-4 text-muted">
                {#if item.no_data_reason}
                  {item.no_data_reason}
                {:else if item.insufficient_history}
                  Insufficient history; showing available real aggregate only.
                {:else}
                  Real meme snapshot history.
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </Card>
  </section>
{/if}
