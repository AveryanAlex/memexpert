<script lang="ts">
  import type { ContentKind, PublicMemeFileRead } from '$lib/api/types';
  import { selectMediaRender } from '$lib/media/render';

  interface Props {
    file: PublicMemeFileRead | null;
    mediaType: ContentKind;
    alt?: string | null;
    variant?: 'card' | 'detail';
    showDownload?: boolean;
  }

  let { file, mediaType, alt = null, variant = 'card', showDownload = true }: Props = $props();
  const media = $derived(selectMediaRender(file));
  const imageAlt = $derived(alt || 'Meme media');
</script>

<div class={`media-panel media-panel-${variant}`} data-has-media={media.hasMedia}>
  {#if media.videoUrl}
    <video
      class="media-asset"
      controls={variant === 'detail'}
      muted
      playsinline
      loop
      preload="metadata"
      poster={media.imageUrl || undefined}
      aria-label={imageAlt}
    >
      <source src={media.videoUrl} type={file?.mime_type || 'video/mp4'} />
    </video>
  {:else if media.imageUrl}
    <img
      class="media-asset"
      src={media.imageUrl}
      alt={imageAlt}
      width={file?.render?.width || file?.width || undefined}
      height={file?.render?.height || file?.height || undefined}
      loading={variant === 'card' ? 'lazy' : 'eager'}
      decoding="async"
    />
  {:else}
    <div class="media-placeholder" aria-label="Media unavailable">
      <span>{mediaType}</span>
      <small>Media unavailable</small>
    </div>
  {/if}

  {#if showDownload && media.downloadUrl}
    <a class="media-download" href={media.downloadUrl} download>Download</a>
  {/if}
</div>
