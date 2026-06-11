<script lang="ts">
  import MemeMedia from '$lib/components/MemeMedia.svelte';
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

  function memeHref(meme: { id: string; seo_page_slug: string | null }): string {
    return `/memes/${meme.seo_page_slug || meme.id}`;
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

  {#if page.items.length > 0}
    <section class="grid" aria-label="Template memes">
      {#each page.items as item (item.meme.id)}
        {@const meme = item.meme}
        <article class="card">
          <a class="card-media-link" href={memeHref(meme)} aria-label={`Open ${meme.caption || 'meme'}`}>
            <MemeMedia file={meme.primary_file} mediaType={meme.media_type} alt={meme.caption} showDownload={false} />
          </a>
          <div class="card-body">
            <p class="caption"><a href={memeHref(meme)}>{meme.caption || meme.tags[0] || 'Untitled meme'}</a></p>
            {#if meme.primary_file?.render?.download_url}
              <a class="download-link" href={meme.primary_file.render.download_url} download>Download media</a>
            {/if}
            <div class="meta" aria-label="Meme metadata">
              <span>{meme.language}</span>
              <span>{meme.like_count} likes</span>
            </div>
          </div>
        </article>
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
