<script lang="ts">
  import type { PublicMemeCardRead, PublicMemeDetailRead } from '$lib/api/types';
  import { selectMediaRender } from '$lib/media/render';
  import { memeTitle } from '$lib/memeActions';
  import { Download } from '@lucide/svelte';

  interface Props {
    meme: PublicMemeCardRead | PublicMemeDetailRead;
    detail?: boolean;
    showDownload?: boolean;
  }

  let { meme, detail = false, showDownload = false }: Props = $props();

  const file = $derived(meme.primary_file);
  const media = $derived(selectMediaRender(file));
  const title = $derived(memeTitle(meme));
</script>

<div
  class={[
    'relative grid place-items-center overflow-hidden bg-[radial-gradient(circle_at_top_left,rgb(255_118_74_/_35%),transparent_35%),linear-gradient(135deg,#252f43,#44516a)] font-black uppercase tracking-[0.16em] text-paper',
    detail ? 'min-h-[22.5rem] rounded-[22px]' : 'min-h-[9.5rem]',
    media.hasMedia ? 'bg-[#101725] p-0' : ''
  ].join(' ')}
  data-has-media={media.hasMedia}
>
  {#if media.videoUrl}
    <video
      class={detail ? 'block size-full min-h-[inherit] object-contain' : 'block size-full min-h-[inherit] object-cover'}
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
      class={detail ? 'block size-full min-h-[inherit] object-contain' : 'block size-full min-h-[inherit] object-cover'}
      src={media.imageUrl}
      alt={title}
      width={file?.render?.width || file?.width || undefined}
      height={file?.render?.height || file?.height || undefined}
      loading={detail ? 'eager' : 'lazy'}
      decoding="async"
    />
  {:else if media.audioUrl}
    <audio class="block size-full min-h-0 self-center p-6" src={media.audioUrl} controls aria-label={title}></audio>
  {:else}
    <div class="grid place-items-center gap-2" aria-label="Media unavailable">
      <span>{meme.media_type}</span>
      <small class="text-xs font-normal normal-case tracking-normal text-paper/75">Media unavailable</small>
    </div>
  {/if}

  {#if showDownload && media.downloadUrl}
    <a class="absolute bottom-3 right-3 inline-flex items-center gap-1 rounded-full bg-paper/95 px-3 py-2 text-xs font-black normal-case tracking-normal text-ink no-underline" href={media.downloadUrl} download>
      <Download class="size-3" aria-hidden="true" />
      Download
    </a>
  {/if}
</div>
