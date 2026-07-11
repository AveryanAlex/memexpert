<script lang="ts">
  import type { AdminMemeRead } from '$lib/api/types';
  import { selectMediaAspectRatio, selectMediaRender, selectVideoSourceType } from '$lib/media/render';

  let {
    meme,
    label = 'Meme preview',
    compact = false
  }: { meme: AdminMemeRead; label?: string; compact?: boolean } = $props();

  const file = $derived(meme.primary_file);
  const media = $derived(selectMediaRender(file));
  const aspectRatio = $derived(selectMediaAspectRatio(file));
</script>

<div
  class={['relative grid place-items-center overflow-hidden rounded-2xl bg-[#101725] text-paper', compact ? 'min-h-40' : 'min-h-72'].join(' ')}
  style:aspect-ratio={aspectRatio}
  data-admin-media-preview
>
  {#if media.videoUrl}
    <video class="block size-full object-contain" controls playsinline preload="metadata" poster={media.imageUrl || undefined} aria-label={label}>
      <source src={media.videoUrl} type={selectVideoSourceType(file)} />
    </video>
  {:else if media.audioUrl}
    <div class="grid w-full gap-3 p-5 text-center">
      <strong>Audio preview</strong>
      <audio class="w-full" src={media.audioUrl} controls preload="metadata" aria-label={label}></audio>
    </div>
  {:else if media.imageUrl}
    <img
      class="block size-full object-contain"
      src={media.imageUrl}
      alt={label}
      width={file?.render?.width || file?.width || undefined}
      height={file?.render?.height || file?.height || undefined}
      loading={compact ? 'lazy' : 'eager'}
      decoding="async"
    />
  {:else}
    <div class="grid gap-2 p-6 text-center">
      <strong>{meme.media_type === 'text' || meme.media_type === 'link' ? 'No media preview' : 'Media unavailable'}</strong>
      <span class="text-sm text-paper/70">Open the detail page for metadata.</span>
    </div>
  {/if}
</div>
