<script lang="ts">
  import { page } from '$app/state';
  import type { ActionData } from './$types';
  import type { PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

  const returnTo = $derived(page.url.pathname);
</script>

{#if data.meme}
  <article class="detail-card">
    <div class="media-panel detail-media">{data.meme.media_type}</div>
    <div class="detail-copy">
      <p class="pill">{data.meme.language} · {data.meme.like_count} likes</p>
      <h1>{data.meme.seo_title || data.meme.caption || 'Meme detail'}</h1>
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
