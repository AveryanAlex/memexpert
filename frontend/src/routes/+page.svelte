<script lang="ts">
  import MemeCard from '$lib/components/MemeCard.svelte';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

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

<section class="collection-home-panel" aria-labelledby="collections-title">
  <div class="collection-home-copy">
    <h2 id="collections-title">Your collections</h2>
    <p class="muted">Use Favorites for quick saves, or create custom collections from a full account.</p>
  </div>

  {#if data.collections}
    <div class="collection-link-row" aria-label="Collection list">
      {#each data.collections.collections as item (item.collection.id)}
        <a class={item.active_save_collection_id === item.collection.id ? 'collection-chip active' : 'collection-chip'} href={`/collection/${item.collection.id}`}>
          <span>{item.collection.title}</span>
          <small>{item.collection.kind}{item.active_save_collection_id === item.collection.id ? ' · active' : ''}</small>
        </a>
      {/each}
    </div>
  {:else}
    <p class="muted">Collections are unavailable until your session is ready.</p>
  {/if}

  {#if form?.collectionError}
    <p class="notice" role="alert">{form.collectionError}</p>
  {:else if form?.successMessage}
    <p class="notice" role="status">
      {form.successMessage}
      {#if form.collectionCreatedId}
        <a href={`/collection/${form.collectionCreatedId}`}>Open it</a>
      {/if}
    </p>
  {/if}

  {#if data.session?.user.account_type === 'full'}
    <form class="inline-form collection-create-form" method="POST" action="?/createCollection">
      <input name="title" placeholder="New collection title" maxlength="120" required aria-label="New collection title" />
      <input name="description" placeholder="Description" aria-label="Collection description" />
      <select name="visibility" aria-label="Collection visibility">
        <option value="private">Private</option>
        <option value="unlisted">Unlisted</option>
        <option value="public">Public</option>
      </select>
      <button type="submit">Create collection</button>
    </form>
  {:else}
    <p class="muted">Connect Telegram to create custom collections and collaborate.</p>
  {/if}
</section>

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
