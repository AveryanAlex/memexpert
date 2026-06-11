<script lang="ts">
  import MemeCard from '$lib/components/MemeCard.svelte';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  const resultStart = $derived(data.page.total === 0 ? 0 : data.offset + 1);
  const resultEnd = $derived(Math.min(data.offset + data.page.items.length, data.page.total));
  const previousOffset = $derived(Math.max(data.offset - data.page.limit, 0));
  const nextOffset = $derived(data.offset + data.page.limit);

  function pageHref(offset: number): string {
    const params = new URLSearchParams();

    if (data.query) {
      params.set('q', data.query);
    }

    if (offset > 0) {
      params.set('offset', String(offset));
    }

    const query = params.toString();
    return query ? `/?${query}` : '/';
  }
</script>

<section class="hero" aria-labelledby="search-title">
  <div>
    <h1 id="search-title">Find the right meme fast.</h1>
    <p class="muted">Search the public MemeXpert catalog with plain text, or browse what is already popular.</p>
  </div>
  <span class="pill">Guest access enabled</span>
</section>

<form class="search-form" method="GET" action="/">
  <input
    aria-label="Search memes"
    name="q"
    type="search"
    placeholder="try: cat reaction, distracted boyfriend, friday mood"
    value={data.query}
  />
  <button type="submit">Search</button>
</form>

{#if data.errorMessage}
  <p class="notice" role="status">{data.errorMessage}</p>
{/if}

<div class="status-row">
  <p class="muted">
    {#if data.query}
      Results for “{data.query}”
    {:else}
      Browsing public memes
    {/if}
  </p>
  <p class="muted">Showing {resultStart}-{resultEnd} of {data.page.total}</p>
</div>

{#if data.page.items.length > 0}
  <section class="grid" aria-label="Meme results">
    {#each data.page.items as item (item.meme.id)}
      <MemeCard meme={item.meme} />
    {/each}
  </section>
{:else if !data.errorMessage}
  <section class="empty-state">
    <h2>No memes found</h2>
    <p class="muted">Try a shorter phrase, a different synonym, or clear the search box to browse.</p>
  </section>
{/if}

<nav class="pagination" aria-label="Pagination">
  {#if data.offset > 0}
    <a class="button-link secondary" href={pageHref(previousOffset)}>Previous</a>
  {/if}
  {#if data.page.has_more}
    <a class="button-link" href={pageHref(nextOffset)}>Next page</a>
  {/if}
</nav>
