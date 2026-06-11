<script lang="ts">
  import type { PublicMemeCardRead, PublicMemeDetailRead } from '$lib/api/types';
  import { selectMediaRender } from '$lib/media/render';
  import { memeTitle } from '$lib/memeActions';

  interface Props {
    meme: PublicMemeCardRead | PublicMemeDetailRead;
    detail?: boolean;
    showDownload?: boolean;
  }

  let { meme, detail = false, showDownload = false }: Props = $props();

  const file = $derived(meme.primary_file);
  const media = $derived(selectMediaRender(file));
  const title = $derived(memeTitle(meme));
  const panelClass = $derived(detail ? 'media-panel media-panel-detail' : 'media-panel');
</script>

<div class={media.hasMedia ? `${panelClass} has-media` : panelClass} data-has-media={media.hasMedia}>
  {#if media.videoUrl}
    <video
      class="media-asset"
      controls={detail}
      muted={!detail}
      playsinline
      loop={!detail}
      preload="metadata"
      poster={media.imageUrl || undefined}
      aria-label={title}
    >
      <source src={media.videoUrl} type={file?.mime_type || 'video/mp4'} />
    </video>
  {:else if media.imageUrl}
    <img
      class="media-asset"
      src={media.imageUrl}
      alt={title}
      width={file?.render?.width || file?.width || undefined}
      height={file?.render?.height || file?.height || undefined}
      loading={detail ? 'eager' : 'lazy'}
      decoding="async"
    />
  {:else if media.audioUrl}
    <audio class="media-asset audio-asset" src={media.audioUrl} controls aria-label={title}></audio>
  {:else}
    <div class="media-placeholder" aria-label="Media unavailable">
      <span>{meme.media_type}</span>
      <small>Media unavailable</small>
    </div>
  {/if}

  {#if showDownload && media.downloadUrl}
    <a class="media-download" href={media.downloadUrl} download>Download</a>
  {/if}
</div>
