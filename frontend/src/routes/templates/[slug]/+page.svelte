<script lang="ts">
  import MemeGrid from '$lib/features/memes/MemeGrid.svelte';
  import TrendAggregateHistory from '$lib/features/trends/TrendAggregateHistory.svelte';
  import TrendSummary from '$lib/features/trends/TrendSummary.svelte';
  import { ActionLink, Card, EmptyState, PageHeader } from '$lib/ui';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  const page = $derived(data.landing?.page);
  const resultStart = $derived(page && page.total > 0 ? data.offset + 1 : 0);
  const resultEnd = $derived(page ? Math.min(data.offset + page.items.length, page.total) : 0);
  const previousOffset = $derived(page ? Math.max(data.offset - page.limit, 0) : 0);
  const nextOffset = $derived(page ? data.offset + page.limit : 0);
  const memes = $derived(page?.items.map((item) => item.meme) ?? []);

  function pageHref(offset: number): string {
    return offset > 0 ? `?offset=${offset}` : '';
  }
</script>

{#if data.landing && page}
  <PageHeader title={data.landing.title} description={data.landing.description} badge="Template page" />

  <div class="my-7 flex flex-wrap justify-between gap-3">
    <p class="m-0 text-muted">Showing {resultStart}-{resultEnd} of {page.total}</p>
    <a href="/" class="text-muted">Search all memes</a>
  </div>

  <Card class="mb-5 grid gap-3 shadow-none" aria-label="Template trend summary">
    <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Template trend</h2>
    {#if data.landing.trend_summary}
      <p class="m-0 text-muted">{data.landing.trend_summary.meme_count} public memes in this aggregate.</p>
      <TrendSummary trend={data.landing.trend_summary.trend} />
      <TrendAggregateHistory summary={data.landing.trend_summary} />
    {:else}
      <p class="m-0 text-muted">No materialized trend data for this template yet.</p>
    {/if}
  </Card>

  {#if page.items.length > 0}
    <MemeGrid {memes} label="Template memes" />
  {:else}
    <EmptyState title="No public memes yet" message="This template exists, but there are no visible memes on this page." />
  {/if}

  <nav class="mt-6 flex flex-wrap gap-2" aria-label="Pagination">
    {#if data.offset > 0}
      <ActionLink variant="secondary" href={pageHref(previousOffset)}>Previous</ActionLink>
    {/if}
    {#if page.has_more}
      <ActionLink href={pageHref(nextOffset)}>Next page</ActionLink>
    {/if}
  </nav>
{:else}
  <EmptyState title="Template unavailable" message={data.errorMessage}>
    <ActionLink href="/">Search public memes</ActionLink>
  </EmptyState>
{/if}
