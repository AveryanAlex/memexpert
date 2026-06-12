<script lang="ts">
  import type { PublicMemeCardRead } from '$lib/api/types';
  import { memeHref, memeTitle } from '$lib/memeActions';
  import Badge from '$lib/ui/Badge.svelte';
  import MemeActionMenu from './MemeActionMenu.svelte';
  import MemeMedia from './MemeMedia.svelte';

  interface Props {
    meme: PublicMemeCardRead;
  }

  let { meme }: Props = $props();

  const href = $derived(memeHref(meme));
  const title = $derived(memeTitle(meme));
</script>

<article class="relative grid min-h-[16.25rem] overflow-hidden rounded-[28px] border border-line bg-paper shadow-warm">
  <div class="absolute right-3 top-3 z-10">
    <MemeActionMenu {meme} {href} compact />
  </div>
  <a class="grid text-inherit no-underline" {href} aria-label={`Open ${title}`}>
    <MemeMedia {meme} />
    <div class="grid content-between gap-4 p-4">
      <p class="m-0 text-lg font-extrabold leading-tight">{title}</p>
      <div class="flex flex-wrap gap-2" aria-label="Meme metadata">
        <Badge>{meme.language}</Badge>
        <Badge>{meme.like_count} likes</Badge>
        {#if meme.primary_file?.width && meme.primary_file.height}
          <Badge>{meme.primary_file.width}x{meme.primary_file.height}</Badge>
        {/if}
      </div>
      {#if meme.tags.length > 0}
        <div class="flex flex-wrap gap-2" aria-label="Tags">
          {#each meme.tags.slice(0, 3) as tag}
            <Badge>#{tag}</Badge>
          {/each}
        </div>
      {/if}
    </div>
  </a>
</article>
