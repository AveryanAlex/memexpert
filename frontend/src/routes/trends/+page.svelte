<script lang="ts">
  import MemeCard from '$lib/components/MemeCard.svelte';
  import TrendSummary from '$lib/components/TrendSummary.svelte';
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

<section class="hero trend-hero" aria-labelledby="trends-title">
  <div>
    <h1 id="trends-title">Public meme trends.</h1>
    <p class="muted">Aggregate launch-scope analytics from MemeXpert activity and source popularity snapshots.</p>
  </div>
  <span class="pill">No per-user data</span>
</section>

<nav class="trend-tabs" aria-label="Trend rankings">
  <a class:active={data.ranking === 'trending'} href={rankingHref('trending')}>Trending</a>
  <a class:active={data.ranking === 'fastest_rising'} href={rankingHref('fastest_rising')}>Fastest rising</a>
  <a class:active={data.ranking === 'most_liked'} href={rankingHref('most_liked')}>Most liked</a>
</nav>

{#if data.errorMessage}
  <p class="notice" role="status">{data.errorMessage}</p>
{/if}

<div class="status-row">
  <p class="muted">Showing {resultStart}-{resultEnd} of {data.page.total}</p>
  <a href="/" class="muted">Search all memes</a>
</div>

{#if data.page.items.length > 0}
  <section class="trend-grid" aria-label="Trend ranked memes">
    {#each data.page.items as item (item.meme.id)}
      <article class="trend-card">
        <MemeCard meme={item.meme} />
        <TrendSummary trend={item.trend} />
      </article>
    {/each}
  </section>
{:else if !data.errorMessage}
  <section class="empty-state">
    <h2>No trend data yet</h2>
    <p class="muted">Trend materialized views are empty. Refresh analytics after events or snapshots are available.</p>
  </section>
{/if}

<nav class="pagination" aria-label="Pagination">
  {#if data.offset > 0}
    <a class="button-link secondary" href={rankingHref(data.ranking, previousOffset)}>Previous</a>
  {/if}
  {#if data.page.has_more}
    <a class="button-link" href={rankingHref(data.ranking, nextOffset)}>Next page</a>
  {/if}
</nav>

<section class="summary-columns" aria-label="Aggregate trend summaries">
  <div class="summary-panel">
    <h2>Tags moving now</h2>
    {#if data.tagSummaries.length > 0}
      {#each data.tagSummaries as summary}
        <a class="summary-row" href={`/tags/${summary.slug}`}>
          <span>{summary.title}</span>
          <small>{summary.meme_count} memes · {summary.trend.trending_score.toFixed(1)} score</small>
        </a>
      {/each}
    {:else}
      <p class="muted">No tag aggregates yet.</p>
    {/if}
  </div>
  <div class="summary-panel">
    <h2>Templates moving now</h2>
    {#if data.templateSummaries.length > 0}
      {#each data.templateSummaries as summary}
        <a class="summary-row" href={`/templates/${summary.slug}`}>
          <span>{summary.title}</span>
          <small>{summary.meme_count} memes · {summary.trend.trending_score.toFixed(1)} score</small>
        </a>
      {/each}
    {:else}
      <p class="muted">No template aggregates yet.</p>
    {/if}
  </div>
</section>
