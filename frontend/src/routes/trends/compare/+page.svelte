<script lang="ts">
  import TrendComparisonChart from '$lib/features/trends/TrendComparisonChart.svelte';
  import type { PublicTrendComparisonSeriesRead } from '$lib/api/types';
  import { ActionLink, Button, Card, EmptyState, FormRow, Input, Notice, PageHeader, Select } from '$lib/ui';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  type ComparisonItemType = 'meme' | 'tag' | 'template';
  type ComparisonFormRow = {
    kind: ComparisonItemType;
    identifier: string;
    original: string;
    edited: boolean;
  };

  let formRows = $state<ComparisonFormRow[]>(initialRows());
  let formRowsKey = $state(initialRowsKey());
  let enhanced = $state(false);
  const hasRequestedItems = $derived(data.items.length > 0);
  const numberFormatter = new Intl.NumberFormat('en');
  const recordedActivityDescription =
    'Recorded activity adds original-source views, reactions, and reposts to MemeExpert views, sends, saves, and favorites. It counts signals, not unique people.';
  const selectedRows = $derived(
    formRows
      .filter((row) => Boolean(serializedItem(row)))
      .map((row) => ({
        ...row,
        title: matchingTitle(row)
      }))
  );
  const activityRows = $derived(
    data.comparison.items.flatMap((item) =>
      hasRecordedActivityDetails(item)
        ? item.points.map((point) => ({ item, point }))
        : []
    )
  );
  const pendingChartItems = $derived(data.comparison.items.filter((item) => !isPlottableSeries(item)));

  $effect(() => {
    enhanced = true;
    const nextKey = rowsKey(data.items, data.comparison.max_items);
    if (nextKey === formRowsKey) return;

    formRows = createFormRows(data.items, data.comparison.max_items);
    formRowsKey = nextKey;
  });

  function formatObservedAt(raw: string | null): string {
    if (!raw) return 'This week';

    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return 'This week';

    return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }).format(date);
  }

  function createFormRows(items: string[], maxItems: number): ComparisonFormRow[] {
    return Array.from({ length: maxItems }, (_, index) => parseFormRow(items[index] ?? ''));
  }

  function initialRows(): ComparisonFormRow[] {
    return createFormRows(data.items, data.comparison.max_items);
  }

  function initialRowsKey(): string {
    return rowsKey(data.items, data.comparison.max_items);
  }

  function parseFormRow(serialized: string): ComparisonFormRow {
    const separatorIndex = serialized.indexOf(':');
    const possibleKind = separatorIndex > 0 ? serialized.slice(0, separatorIndex) : null;

    if (possibleKind === 'meme' || possibleKind === 'tag' || possibleKind === 'template') {
      return {
        kind: possibleKind,
        identifier: serialized.slice(separatorIndex + 1),
        original: serialized,
        edited: false
      };
    }

    return { kind: 'meme', identifier: serialized, original: serialized, edited: false };
  }

  function rowsKey(items: string[], maxItems: number): string {
    return JSON.stringify({ items, maxItems });
  }

  function serializedItem(row: ComparisonFormRow): string {
    if (!row.edited) return row.original;

    const identifier = row.identifier.trim();
    return identifier ? `${row.kind}:${identifier}` : '';
  }

  function markEdited(row: ComparisonFormRow): void {
    row.edited = true;
  }

  function matchingTitle(row: ComparisonFormRow): string {
    return (
      data.comparison.items.find(
        (item) =>
          item.kind === row.kind &&
          (item.value === row.identifier ||
            (item.kind === 'meme' &&
              (item.meme?.id === row.identifier || item.meme?.seo_page_slug === row.identifier)))
      )?.title ?? row.identifier
    );
  }

  function itemTypeLabel(kind: string): string {
    if (kind === 'tag') return 'Tag';
    if (kind === 'template') return 'Template';
    return 'Meme';
  }

  /** Uses the same unweighted source-plus-MemeExpert signal count as the charts. */
  function recordedActivity(point: PublicTrendComparisonSeriesRead['points'][number]): number {
    return sourceActivity(point) + memeExpertActivity(point);
  }

  function sourceActivity(point: PublicTrendComparisonSeriesRead['points'][number]): number {
    return count(point.source_views) + count(point.source_reactions) + count(point.source_reposts);
  }

  function memeExpertActivity(point: PublicTrendComparisonSeriesRead['points'][number]): number {
    return count(point.platform_views) + count(point.platform_sends) + count(point.platform_saves) + count(point.platform_likes);
  }

  function isPlottableSeries(item: PublicTrendComparisonSeriesRead): boolean {
    return item.points.length >= 2 && hasRecordedActivityDetails(item);
  }

  function hasRecordedActivityDetails(item: PublicTrendComparisonSeriesRead): boolean {
    return item.points.some((point) => recordedActivity(point) > 0);
  }

  function formatCount(value: number): string {
    return numberFormatter.format(value);
  }

  function count(value: number | null | undefined): number {
    return typeof value === 'number' && Number.isFinite(value) ? Math.max(value, 0) : 0;
  }
</script>

<PageHeader
  title="Compare what is catching on."
  description="Pick a few memes, tags, or templates to compare their recorded activity over time."
  badge="Compare"
>
  <ActionLink href="/trends" variant="secondary">Back to trends</ActionLink>
  <ActionLink href="/trends/timeline" variant="secondary">Browse by time</ActionLink>
</PageHeader>

{#if data.errorMessage}
  <Notice>{data.errorMessage}</Notice>
{/if}

<Card class="mb-6 grid gap-4 shadow-none">
  <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Choose what to compare</h2>
  <p class="m-0 text-muted">Pick the item type, then enter its name or identifier. Add up to {data.comparison.max_items} items.</p>
  <form class="grid gap-3" method="GET" action="/trends/compare">
    <noscript>
      <style>
        .trend-comparison-enhanced { display: none !important; }
      </style>
      <div class="grid gap-3 rounded-xl border border-line bg-soft/50 p-3">
        <p class="m-0 text-sm text-muted">Without JavaScript, enter comparison items from a shared link.</p>
        {#each formRows as row, index (`comparison-fallback-${index}`)}
          <FormRow label={`Comparison item ${index + 1}`}>
            <Input
              id={`comparison-item-${index}`}
              name="item"
              value={serializedItem(row)}
              placeholder="Comparison item"
            />
          </FormRow>
        {/each}
      </div>
    </noscript>
    <div class="trend-comparison-enhanced grid gap-3">
      {#each formRows as row, index (`compare-input-${index}`)}
        <fieldset class="grid gap-3 rounded-xl border border-line bg-soft/50 p-3">
          <legend class="px-1 text-sm font-extrabold text-ink">Item {index + 1}</legend>
          <input type="hidden" name="item" value={serializedItem(row)} disabled={!enhanced} />
          <div class="grid gap-3 sm:grid-cols-[minmax(10rem,0.7fr)_minmax(0,1fr)]">
            <FormRow label="Item type">
              <Select
                id={`comparison-kind-${index}`}
                bind:value={row.kind}
                onchange={() => markEdited(row)}
              >
                <option value="meme">Meme</option>
                <option value="tag">Tag</option>
                <option value="template">Template</option>
              </Select>
            </FormRow>
            <FormRow label="Name or identifier">
              <Input
                id={`comparison-identifier-${index}`}
                bind:value={row.identifier}
                oninput={() => markEdited(row)}
                placeholder="Name or identifier"
              />
            </FormRow>
          </div>
        </fieldset>
      {/each}
    </div>
    <div class="flex flex-wrap gap-2">
      <Button type="submit">Compare</Button>
      <ActionLink href="/trends/compare" variant="ghost">Clear</ActionLink>
    </div>
  </form>
  {#if selectedRows.length > 0}
    <div class="grid gap-2" aria-label="Selected items">
      <p class="m-0 text-sm font-extrabold text-ink">Selected items</p>
      <ul class="m-0 flex list-none flex-wrap gap-2 p-0">
        {#each selectedRows as row, index (`selected:${index}:${serializedItem(row)}`)}
          <li class="rounded-full border border-line bg-paper px-3 py-1.5 text-sm font-semibold text-ink">
            {itemTypeLabel(row.kind)} · {row.title}
          </li>
        {/each}
      </ul>
    </div>
  {/if}
</Card>

{#if !hasRequestedItems}
  <EmptyState title="Pick a few things to compare" message="Start with a meme, tag, or template you want to explore." />
{:else if data.comparison.items.length === 0 && !data.errorMessage}
  <EmptyState title="Nothing to compare just yet" message="Try another name or identifier and see what is catching on." />
{:else}
  <section aria-label="Trend comparison results">
    <Card class="grid gap-6 shadow-none">
      <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">How they compare</h2>
      <TrendComparisonChart series={data.comparison.items} />
      {#if pendingChartItems.length > 0}
        <p class="m-0 text-sm text-muted">Some picks will join the chart once they have two recorded activity moments.</p>
      {/if}
      <div class="overflow-x-auto rounded-xl border border-line bg-paper">
        <table class="w-full min-w-[680px] border-collapse text-left text-sm">
          <caption class="sr-only">Recorded activity details for the comparison</caption>
          <thead>
            <tr class="border-b border-line text-muted">
              <th class="py-3 pl-4 pr-3" scope="col">Item</th>
              <th class="py-3 pr-3" scope="col">Type</th>
              <th class="py-3 pr-3" scope="col">Date</th>
              <th class="py-3 pr-3" scope="col">Recorded activity</th>
              <th class="py-3 pr-3" scope="col">Original sources</th>
              <th class="py-3 pr-4" scope="col">MemeExpert</th>
            </tr>
          </thead>
          <tbody>
            {#each activityRows as row (`row-${row.item.kind}:${row.item.value}:${row.point.observed_at ?? 'current'}:${recordedActivity(row.point)}`)}
              <tr class="border-b border-line/70 align-top last:border-b-0">
                <th class="py-3 pl-4 pr-3 font-extrabold" scope="row">{row.item.title}</th>
                <td class="py-3 pr-3">{itemTypeLabel(row.item.kind)}</td>
                <td class="py-3 pr-3">{formatObservedAt(row.point.observed_at)}</td>
                <td class="py-3 pr-3 tabular-nums">{formatCount(recordedActivity(row.point))} signals</td>
                <td class="py-3 pr-3 tabular-nums">{formatCount(sourceActivity(row.point))}</td>
                <td class="py-3 pr-4 tabular-nums">{formatCount(memeExpertActivity(row.point))}</td>
              </tr>
            {:else}
              <tr>
                <td class="p-4 text-muted" colspan="6">Recorded activity details will appear as items collect signals.</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </Card>
  </section>
{/if}
