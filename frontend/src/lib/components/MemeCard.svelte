<script lang="ts">
  import type { PublicMemeCardRead } from '$lib/api/types';
  import { memeHref, memeTitle } from '$lib/memeActions';
  import MemeActionMenu from './MemeActionMenu.svelte';
  import MemeMedia from './MemeMedia.svelte';

  interface Props {
    meme: PublicMemeCardRead;
  }

  let { meme }: Props = $props();

  const href = $derived(memeHref(meme));
  const title = $derived(memeTitle(meme));
</script>

<article class="card meme-card">
  <div class="meme-card-menu">
    <MemeActionMenu {meme} {href} compact />
  </div>
  <a class="meme-card-link" {href} aria-label={`Open ${title}`}>
    <MemeMedia {meme} />
    <div class="card-body">
      <p class="caption">{title}</p>
      <div class="meta" aria-label="Meme metadata">
        <span>{meme.language}</span>
        <span>{meme.like_count} likes</span>
        {#if meme.primary_file?.width && meme.primary_file.height}
          <span>{meme.primary_file.width}x{meme.primary_file.height}</span>
        {/if}
      </div>
      {#if meme.tags.length > 0}
        <div class="tags" aria-label="Tags">
          {#each meme.tags.slice(0, 3) as tag}
            <span class="tag">#{tag}</span>
          {/each}
        </div>
      {/if}
    </div>
  </a>
</article>
