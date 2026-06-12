<script lang="ts">
  import MemeCard from '$lib/features/memes/MemeCard.svelte';
  import TrendSummary from '$lib/features/trends/TrendSummary.svelte';
  import { ActionLink, Card, EmptyState, Notice, PageHeader } from '$lib/ui';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  const resultStart = $derived(data.page.total === 0 ? 0 : data.offset + 1);
  const resultEnd = $derived(Math.min(data.offset + data.page.items.length, data.page.total));
  const previousOffset = $derived(Math.max(data.offset - data.page.limit, 0));
  const nextOffset = $derived(data.offset + data.page.limit);

  function rankingHref(ranking: string, offset = 0): string {
    const params = new URLSearchParams({ ranking });
    if (offset > 0) {
      params.set('offset', String(offset));
    }
    return `/trends?${params.toString()}`;
  }
</script>

<PageHeader title="Public meme trends." description="Aggregate launch-scope analytics from MemeXpert activity and source popularity snapshots." badge="No per-user data">
  <ActionLink href="/trends/compare" variant="secondary">Compare trends</ActionLink>
  <ActionLink href="/trends/timeline" variant="secondary">Timeline</ActionLink>
</PageHeader>

<nav class="mb-6 flex flex-wrap gap-2" aria-label="Trend rankings">
  <a class={data.ranking === 'trending' ? 'rounded-full bg-ink px-4 py-3 font-extrabold text-paper no-underline' : 'rounded-full border border-line bg-paper px-4 py-3 font-extrabold text-ink no-underline'} href={rankingHref('trending')}>Trending</a>
  <a class={data.ranking === 'fastest_rising' ? 'rounded-full bg-ink px-4 py-3 font-extrabold text-paper no-underline' : 'rounded-full border border-line bg-paper px-4 py-3 font-extrabold text-ink no-underline'} href={rankingHref('fastest_rising')}>Fastest rising</a>
  <a class={data.ranking === 'most_liked' ? 'rounded-full bg-ink px-4 py-3 font-extrabold text-paper no-underline' : 'rounded-full border border-line bg-paper px-4 py-3 font-extrabold text-ink no-underline'} href={rankingHref('most_liked')}>Most liked</a>
</nav>

{#if data.errorMessage}
  <Notice>{data.errorMessage}</Notice>
{/if}

<div class="my-7 flex flex-wrap justify-between gap-3">
  <p class="m-0 text-muted">Showing {resultStart}-{resultEnd} of {data.page.total}</p>
  <a href="/" class="text-muted">Search all memes</a>
</div>

{#if data.page.items.length > 0}
  <section class="grid grid-cols-1 gap-4 md:grid-cols-3" aria-label="Trend ranked memes">
    {#each data.page.items as item (item.meme.id)}
      <Card class="grid gap-3 p-4 shadow-none">
        <MemeCard meme={item.meme} />
        <TrendSummary trend={item.trend} />
      </Card>
    {/each}
  </section>
{:else if !data.errorMessage}
  <EmptyState title="No trend data yet" message="Trend materialized views are empty. Refresh analytics after events or snapshots are available." />
{/if}

<nav class="mt-6 flex flex-wrap gap-2" aria-label="Pagination">
  {#if data.offset > 0}
    <ActionLink variant="secondary" href={rankingHref(data.ranking, previousOffset)}>Previous</ActionLink>
  {/if}
  {#if data.page.has_more}
    <ActionLink href={rankingHref(data.ranking, nextOffset)}>Next page</ActionLink>
  {/if}
</nav>

<section class="mt-7 grid gap-4 md:grid-cols-2" aria-label="Aggregate trend summaries">
  <Card class="grid gap-3 shadow-none">
    <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Tags moving now</h2>
    {#if data.tagSummaries.length > 0}
      {#each data.tagSummaries as summary}
        <a class="flex items-center justify-between gap-3 rounded-[18px] border border-line bg-paper px-4 py-3 font-extrabold no-underline" href={`/tags/${summary.slug}`}>
          <span>{summary.title}</span>
          <small class="text-muted">{summary.meme_count} memes · {summary.trend.trending_score.toFixed(1)} score</small>
        </a>
      {/each}
    {:else}
      <p class="m-0 text-muted">No tag aggregates yet.</p>
    {/if}
  </Card>
  <Card class="grid gap-3 shadow-none">
    <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Templates moving now</h2>
    {#if data.templateSummaries.length > 0}
      {#each data.templateSummaries as summary}
        <a class="flex items-center justify-between gap-3 rounded-[18px] border border-line bg-paper px-4 py-3 font-extrabold no-underline" href={`/templates/${summary.slug}`}>
          <span>{summary.title}</span>
          <small class="text-muted">{summary.meme_count} memes · {summary.trend.trending_score.toFixed(1)} score</small>
        </a>
      {/each}
    {:else}
      <p class="m-0 text-muted">No template aggregates yet.</p>
    {/if}
  </Card>
</section>
