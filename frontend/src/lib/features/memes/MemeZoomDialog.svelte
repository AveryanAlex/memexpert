<script lang="ts">
  import type { PublicMemeCardRead } from '$lib/api/types';
  import { selectMediaZoomImage } from '$lib/media/render';
  import { memeTitle } from '$lib/memeActions';
  import * as Dialog from '$lib/ui/dialog';
  import { X, ZoomIn } from '@lucide/svelte';

  interface Props {
    meme: PublicMemeCardRead;
    showTrigger?: boolean;
  }

  let { meme, showTrigger = true }: Props = $props();

  const title = $derived(memeTitle(meme));
  const zoomImageUrl = $derived(meme.media_type === 'image' || meme.media_type === 'gif' ? selectMediaZoomImage(meme.primary_file) : null);
  const titleId = $derived(`meme-zoom-title-${meme.id}`);
  const descriptionId = $derived(`meme-zoom-description-${meme.id}`);
</script>

{#if zoomImageUrl && showTrigger}
  <Dialog.Root>
    <Dialog.Trigger
      type="button"
      class="absolute right-3 top-3 z-10 !hidden !size-10 !place-items-center !rounded-full !border !border-white/30 !bg-black/65 !p-0 !text-white shadow-lg backdrop-blur-sm hover:!bg-black/80 min-[600px]:!grid"
      aria-label={`Enlarge ${title}`}
      title="Enlarge image"
    >
      <ZoomIn class="size-5" aria-hidden="true" />
    </Dialog.Trigger>

    <Dialog.Content
      class="!block !h-auto !w-fit !max-h-[calc(100dvh-2rem)] !max-w-[calc(100vw-2rem)] !gap-0 !overflow-hidden !rounded-[18px] !border-white/15 !bg-[#080b12] !p-0 !text-white"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
    >
      <Dialog.Title id={titleId} class="sr-only">{title}</Dialog.Title>
      <Dialog.Description id={descriptionId} class="sr-only">Expanded image preview. Press Escape or use the close button to return to the meme feed.</Dialog.Description>
      <div class="flex justify-end p-2">
        <Dialog.Close
          class="grid size-11 place-items-center rounded-full border border-white/25 bg-black/70 text-white shadow-lg backdrop-blur-sm hover:bg-black/90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
          aria-label="Close enlarged image"
          title="Close"
        >
          <X class="size-5" aria-hidden="true" />
        </Dialog.Close>
      </div>
      <div class="grid place-items-center px-2 pb-2 sm:px-4 sm:pb-4">
        <img
          class="block h-auto w-auto min-h-0 max-h-[calc(100dvh-7rem)] max-w-[calc(100vw-3.25rem)] object-contain sm:max-h-[calc(100dvh-8rem)] sm:max-w-[calc(100vw-5.25rem)]"
          src={zoomImageUrl}
          alt={`Enlarged ${title}`}
          decoding="async"
        />
      </div>
    </Dialog.Content>
  </Dialog.Root>
{/if}
