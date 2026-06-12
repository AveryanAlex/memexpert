<script lang="ts">
  import { page } from '$app/state';
  import MemeActionMenu from '$lib/components/MemeActionMenu.svelte';
  import MemeMedia from '$lib/components/MemeMedia.svelte';
  import TrendSparkline from '$lib/components/TrendSparkline.svelte';
  import TrendSummary from '$lib/components/TrendSummary.svelte';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

  const returnTo = $derived(page.url.pathname);
</script>

{#if data.meme}
  <article class="detail-card">
    <MemeMedia meme={data.meme} detail />
    <div class="detail-copy">
      <p class="pill">{data.meme.language} · {data.meme.like_count} likes</p>
      <h1>{data.meme.seo_title || data.meme.caption || 'Meme detail'}</h1>
      <MemeActionMenu meme={data.meme} showPrimary />
      {#if data.meme.seo_description}
        <p>{data.meme.seo_description}</p>
      {:else if data.meme.caption}
        <p>{data.meme.caption}</p>
      {/if}
      {#if data.meme.ocr_text}
        <p class="muted">Detected text: {data.meme.ocr_text}</p>
      {/if}
      {#if data.meme.seo_body_text}
        <p>{data.meme.seo_body_text}</p>
      {/if}
      <div class="meta">
        <span>{data.meme.files.length || (data.meme.primary_file ? 1 : 0)} files</span>
        <span>score {data.meme.popularity_score.toFixed(1)}</span>
        {#if data.meme.primary_file?.mime_type}
          <span>{data.meme.primary_file.mime_type}</span>
        {/if}
      </div>
      {#if data.meme.tags.length > 0}
        <div class="tags" aria-label="Tags">
          {#each data.meme.tags as tag}
            <span class="tag"><a href={`/tags/${tag}`}>#{tag}</a></span>
          {/each}
        </div>
      {/if}
      <section class="trend-section" aria-label="Popularity trend">
        <h2>Popularity trend</h2>
        <TrendSummary trend={data.popularity?.trend ?? null} />
        {#if data.popularity}
          <TrendSparkline points={data.popularity.sparkline} />
        {:else}
          <p class="muted">Popularity analytics are not available for this meme yet.</p>
        {/if}
      </section>
      <div class="detail-actions">
        <form method="POST" action="?/favorite">
          <input type="hidden" name="memeId" value={data.meme.id} />
          <button type="submit">Save to favorites</button>
        </form>
        <a class="button-link secondary" href="/">Back to search</a>
      </div>
      {#if form?.message}
        <p class="notice" role="status">{form.message}</p>
      {/if}
      {#if form?.status === 'saved' && form.showConnectTelegram}
        <section class="benefit-cta" aria-label="Keep favorites">
          <p class="caption">Keep this save beyond this browser.</p>
          <a class="button-link" href={`/account/telegram?returnTo=${encodeURIComponent(returnTo)}`}>
            Connect Telegram to keep saves/favorites
          </a>
        </section>
      {/if}
    </div>
  </article>
{:else}
  <section class="empty-state">
    <h1>Meme unavailable</h1>
    <p class="muted">{data.unavailableMessage}</p>
    <div class="detail-actions">
      <a class="button-link" href="/">Search public memes</a>
    </div>
  </section>
{/if}
