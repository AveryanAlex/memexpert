<script lang="ts">
  import type { PublicMemeCardRead, PublicMemeDetailRead } from '$lib/api/types';
  import { selectFeedPreviewAspectRatio, selectImageLoading, selectMediaPreload, selectMediaRender, selectVideoSourceType } from '$lib/media/render';
  import { memeTitle } from '$lib/memeActions';
  import { Download } from '@lucide/svelte';

  interface Props {
    meme: PublicMemeCardRead | PublicMemeDetailRead;
    detail?: boolean;
    preview?: boolean;
    showDownload?: boolean;
  }

  let { meme, detail = false, preview = false, showDownload = false }: Props = $props();

  const file = $derived(meme.primary_file);
  const media = $derived(selectMediaRender(file));
  const title = $derived(memeTitle(meme));
  const feedPreview = $derived(preview && !detail);
  const previewAspectRatio = $derived(feedPreview && media.hasMedia ? selectFeedPreviewAspectRatio(file) : null);
  const mediaClass = $derived(
    detail || feedPreview
      ? 'block size-full min-w-0 max-w-full min-h-[inherit] object-contain'
      : 'block size-full min-w-0 max-w-full min-h-[inherit] object-cover'
  );
  const imageLoading = $derived(selectImageLoading(detail));
  const mediaPreload = $derived(selectMediaPreload(detail));
</script>

<div
  class={[
    'relative grid w-full min-w-0 max-w-full place-items-center overflow-hidden bg-[radial-gradient(circle_at_top_left,rgb(255_118_74_/_35%),transparent_35%),linear-gradient(135deg,#252f43,#44516a)] font-black uppercase tracking-[0.16em] text-media-foreground',
    detail ? 'min-h-[22.5rem] rounded-[22px]' : 'min-h-[9.5rem]',
    media.hasMedia ? 'bg-[#101725] p-0' : ''
  ].join(' ')}
  style:aspect-ratio={previewAspectRatio}
  data-has-media={media.hasMedia}
>
  {#if media.videoUrl}
    <video
      class={mediaClass}
      controls={detail}
      muted={!detail}
      playsinline
      loop={!detail}
      preload={mediaPreload}
      poster={media.imageUrl || undefined}
      aria-label={title}
    >
      <source src={media.videoUrl} type={selectVideoSourceType(file)} />
    </video>
  {:else if media.imageUrl}
    <img
      class={mediaClass}
      src={media.imageUrl}
      alt={title}
      width={file?.render?.width || file?.width || undefined}
      height={file?.render?.height || file?.height || undefined}
      loading={imageLoading}
      decoding="async"
    />
  {:else if media.audioUrl}
    {#if detail}
      <audio class="block size-full min-h-0 self-center p-6" src={media.audioUrl} controls preload={mediaPreload} aria-label={title}></audio>
    {:else}
      <div class="grid place-items-center gap-2 text-center" aria-label={title}>
        <span>{meme.media_type}</span>
        <small class="px-4 text-xs font-normal normal-case tracking-normal text-media-muted">Audio available on the detail page</small>
      </div>
    {/if}
  {:else}
    <div class="grid place-items-center gap-2" aria-label="Media unavailable">
      <span>{meme.media_type}</span>
      <small class="text-xs font-normal normal-case tracking-normal text-media-muted">Media unavailable</small>
    </div>
  {/if}

  {#if showDownload && media.downloadUrl}
    <a class="absolute bottom-3 right-3 inline-flex items-center gap-1 rounded-full bg-white/95 px-3 py-2 text-xs font-black normal-case tracking-normal text-zinc-900 no-underline" href={media.downloadUrl} download>
      <Download class="size-3" aria-hidden="true" />
      Download
    </a>
  {/if}
</div>
