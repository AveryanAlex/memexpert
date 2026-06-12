<script lang="ts">
  import type { PublicMemeCardRead } from '$lib/api/types';
  import { memeHref, memeTitle } from '$lib/memeActions';
  import Badge from '$lib/ui/Badge.svelte';
  import { cn, focusRing } from '$lib/ui/styles';
  import MemeActionMenu from './MemeActionMenu.svelte';
  import MemeMedia from './MemeMedia.svelte';

  interface Props {
    meme: PublicMemeCardRead;
    position?: number;
    total?: number;
  }

  let { meme, position, total }: Props = $props();

  const href = $derived(memeHref(meme));
  const title = $derived(memeTitle(meme));
  const titleId = $derived(`meme-card-title-${meme.id}`);
</script>

<article
  class="relative grid min-h-[16.25rem] overflow-hidden rounded-[28px] border border-line bg-paper shadow-warm"
  role={position ? 'listitem' : undefined}
  aria-posinset={position}
  aria-setsize={total}
  aria-labelledby={titleId}
>
  <a class={cn('grid rounded-[28px] text-inherit no-underline', focusRing)} {href} aria-label={`Open ${title}`}>
    <MemeMedia {meme} preview />
    <div class="grid content-between gap-4 p-4">
      <p id={titleId} class="m-0 text-lg font-extrabold leading-tight">{title}</p>
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
  <div class="absolute right-3 top-3 z-10">
    <MemeActionMenu {meme} {href} compact />
  </div>
</article>
