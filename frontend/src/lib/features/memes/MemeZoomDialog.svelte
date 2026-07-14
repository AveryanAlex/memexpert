<script lang="ts">
  import type { PublicMemeCardRead } from '$lib/api/types';
  import { selectMediaZoomImage } from '$lib/media/render';
  import { memeTitle } from '$lib/memeActions';
  import * as Dialog from '$lib/ui/dialog';
  import { X, ZoomIn } from '@lucide/svelte';

  interface Props {
    meme: PublicMemeCardRead;
  }

  let { meme }: Props = $props();

  const title = $derived(memeTitle(meme));
  const zoomImageUrl = $derived(meme.media_type === 'image' || meme.media_type === 'gif' ? selectMediaZoomImage(meme.primary_file) : null);
  const titleId = $derived(`meme-zoom-title-${meme.id}`);
  const descriptionId = $derived(`meme-zoom-description-${meme.id}`);
</script>

{#if zoomImageUrl}
  <Dialog.Root>
    <Dialog.Trigger
      type="button"
      class="absolute right-3 top-3 z-10 !grid !size-10 !place-items-center !rounded-full !border !border-white/30 !bg-black/65 !p-0 !text-white shadow-lg backdrop-blur-sm hover:!bg-black/80"
      aria-label={`Enlarge ${title}`}
      title="Enlarge image"
    >
      <ZoomIn class="size-5" aria-hidden="true" />
    </Dialog.Trigger>

    <Dialog.Content
      class="!h-[94dvh] !max-h-[64rem] !w-[96vw] !max-w-[96rem] !gap-0 !overflow-hidden !rounded-[18px] !border-white/15 !bg-[#080b12] !p-0 !text-white"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
    >
      <Dialog.Title id={titleId} class="sr-only">{title}</Dialog.Title>
      <Dialog.Description id={descriptionId} class="sr-only">Expanded image preview. Press Escape or use the close button to return to the meme feed.</Dialog.Description>
      <Dialog.Close
        class="absolute right-3 top-3 z-10 grid size-11 place-items-center rounded-full border border-white/25 bg-black/70 text-white shadow-lg backdrop-blur-sm hover:bg-black/90"
        aria-label="Close enlarged image"
        title="Close"
      >
        <X class="size-5" aria-hidden="true" />
      </Dialog.Close>
      <div class="grid size-full min-h-0 place-items-center overflow-hidden p-2 sm:p-4">
        <img class="size-full object-contain" src={zoomImageUrl} alt={`Enlarged ${title}`} decoding="async" />
      </div>
    </Dialog.Content>
  </Dialog.Root>
{/if}
