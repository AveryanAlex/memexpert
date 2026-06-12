<script lang="ts">
  import MemeCard from '$lib/components/MemeCard.svelte';
  import TrendSummary from '$lib/components/TrendSummary.svelte';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  const page = $derived(data.landing?.page);
  const resultStart = $derived(page && page.total > 0 ? data.offset + 1 : 0);
  const resultEnd = $derived(page ? Math.min(data.offset + page.items.length, page.total) : 0);
  const previousOffset = $derived(page ? Math.max(data.offset - page.limit, 0) : 0);
  const nextOffset = $derived(page ? data.offset + page.limit : 0);

  function pageHref(offset: number): string {
    return offset > 0 ? `?offset=${offset}` : '';
  }
</script>

{#if data.landing && page}
  <section class="hero" aria-labelledby="landing-title">
    <div>
      <h1 id="landing-title">{data.landing.title}</h1>
      {#if data.landing.description}
        <p class="muted">{data.landing.description}</p>
      {/if}
    </div>
    <span class="pill">Template page</span>
  </section>

  <div class="status-row">
    <p class="muted">Showing {resultStart}-{resultEnd} of {page.total}</p>
    <a href="/" class="muted">Search all memes</a>
  </div>

  <section class="trend-section" aria-label="Template trend summary">
    <h2>Template trend</h2>
    {#if data.landing.trend_summary}
      <p class="muted">{data.landing.trend_summary.meme_count} public memes in this aggregate.</p>
      <TrendSummary trend={data.landing.trend_summary.trend} />
    {:else}
      <p class="muted">No materialized trend data for this template yet.</p>
    {/if}
  </section>

  {#if page.items.length > 0}
    <section class="grid" aria-label="Template memes">
      {#each page.items as item (item.meme.id)}
        <MemeCard meme={item.meme} />
      {/each}
    </section>
  {:else}
    <section class="empty-state">
      <h2>No public memes yet</h2>
      <p class="muted">This template exists, but there are no visible memes on this page.</p>
    </section>
  {/if}

  <nav class="pagination" aria-label="Pagination">
    {#if data.offset > 0}
      <a class="button-link secondary" href={pageHref(previousOffset)}>Previous</a>
    {/if}
    {#if page.has_more}
      <a class="button-link" href={pageHref(nextOffset)}>Next page</a>
    {/if}
  </nav>
{:else}
  <section class="empty-state">
    <h1>Template unavailable</h1>
    <p class="muted">{data.errorMessage}</p>
    <a class="button-link" href="/">Search public memes</a>
  </section>
{/if}
