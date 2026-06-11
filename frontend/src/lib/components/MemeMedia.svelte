<script lang="ts">
  import type { PublicMemeCardRead, PublicMemeDetailRead } from '$lib/api/types';
  import { memeRenderUrl, memeTitle } from '$lib/memeActions';

  interface Props {
    meme: PublicMemeCardRead | PublicMemeDetailRead;
    detail?: boolean;
  }

  let { meme, detail = false }: Props = $props();

  const renderUrl = $derived(memeRenderUrl(meme));
  const title = $derived(memeTitle(meme));
  const panelClass = $derived(detail ? 'media-panel detail-media' : 'media-panel');
</script>

<div class={renderUrl ? `${panelClass} has-media` : panelClass}>
  {#if renderUrl && meme.media_type === 'video'}
    <!-- svelte-ignore a11y_media_has_caption: meme media captions are not part of the current API contract. -->
    <video class="media-asset" src={renderUrl} controls preload="metadata" aria-label={title}></video>
  {:else if renderUrl && meme.media_type === 'audio'}
    <audio class="media-asset audio-asset" src={renderUrl} controls aria-label={title}></audio>
  {:else if renderUrl}
    <img class="media-asset" src={renderUrl} alt={title} loading={detail ? 'eager' : 'lazy'} />
  {:else}
    <span>{meme.media_type}</span>
  {/if}
</div>
