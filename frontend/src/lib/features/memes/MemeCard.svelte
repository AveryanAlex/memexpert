<script lang="ts">
  import { browser } from '$app/environment';
  import { recordMemeDetailClick, recordMemeImpression } from '$lib/api/client';
  import type { MemeResultAttributionRead, PublicMemeCardRead } from '$lib/api/types';
  import { selectMediaRender } from '$lib/media/render';
  import { memeActionAttributionBody, memeHref, memeTitle } from '$lib/memeActions';
  import { cn, focusRing } from '$lib/ui/styles';
  import { ExternalLink } from '@lucide/svelte';
  import MemeActionMenu from './MemeActionMenu.svelte';
  import MemeMedia from './MemeMedia.svelte';
  import MemeZoomDialog from './MemeZoomDialog.svelte';
  import type { MemeVideoPreviewMode } from './meme-video';

  interface Props {
    meme: PublicMemeCardRead;
    attribution?: MemeResultAttributionRead | null;
    position?: number;
    total?: number;
    showAccessMarkers?: boolean;
    videoPreviewMode?: MemeVideoPreviewMode;
  }

  let {
    meme,
    attribution = null,
    position,
    total,
    showAccessMarkers = false,
    videoPreviewMode = 'poster'
  }: Props = $props();
  let cardElement = $state<HTMLElement>();
  let recordedImpressionFor = $state<string | null>(null);

  const href = $derived(memeHref(meme, attribution));
  const title = $derived(memeTitle(meme));
  const showTitle = $derived(title !== 'Untitled meme');
  const titleId = $derived(`meme-card-title-${meme.id}`);
  const accessVisibility = $derived(meme.viewer_access?.visibility ?? 'public');
  const actionBody = $derived(memeActionAttributionBody(attribution));
  const telemetryRequest = $derived({ fetch, memeId: meme.id, body: actionBody, keepalive: true });
  const isVideo = $derived(Boolean(selectMediaRender(meme.primary_file).videoUrl));

  $effect(() => {
    if (!browser || !cardElement || recordedImpressionFor === meme.id || typeof IntersectionObserver === 'undefined') return;

    const observedMemeId = meme.id;
    const observer = new IntersectionObserver(
      (entries) => {
        if (recordedImpressionFor === observedMemeId || !entries.some((entry) => entry.isIntersecting)) return;

        recordedImpressionFor = observedMemeId;
        observer.disconnect();
        void recordMemeImpression(telemetryRequest).catch((error) => logTelemetryFailure('impression', error));
      },
      { threshold: 0.25 }
    );
    observer.observe(cardElement);

    return () => observer.disconnect();
  });

  function handleDetailClick() {
    if (!browser) return;

    void recordMemeDetailClick(telemetryRequest).catch((error) => logTelemetryFailure('detail-click', error));
  }

  function logTelemetryFailure(action: 'detail-click' | 'impression', error: unknown) {
    console.warn('Meme card telemetry failed.', { action, memeId: meme.id, error });
  }
</script>

<article
  bind:this={cardElement}
  class="relative w-full min-w-0 max-w-full overflow-hidden rounded-xl border border-line bg-paper"
  role={position ? 'listitem' : undefined}
  aria-posinset={position}
  aria-setsize={total}
  aria-labelledby={showTitle ? titleId : undefined}
>
  {#if isVideo}
    <div class="relative">
      <a
        class={cn(
          'absolute right-3 top-3 z-20 grid size-10 place-items-center rounded-full bg-ink/75 text-paper shadow-overlay backdrop-blur-sm transition hover:bg-ink',
          focusRing
        )}
        {href}
        aria-label={`Open ${title}`}
        title="Open meme"
        onclick={handleDetailClick}
      >
        <ExternalLink class="size-5" aria-hidden="true" />
      </a>
      <MemeMedia {meme} preview {videoPreviewMode} />
    </div>
    {#if showTitle || (showAccessMarkers && accessVisibility !== 'public')}
      <div class="flex items-start justify-between gap-3 px-3 pb-3 pt-2.5">
        {#if showTitle}
          <a
            id={titleId}
            class={cn('m-0 line-clamp-2 text-sm font-semibold leading-snug text-ink no-underline sm:text-base', focusRing)}
            {href}
            onclick={handleDetailClick}
          >{title}</a>
        {/if}
        {#if showAccessMarkers && accessVisibility !== 'public'}
          <span class="ml-auto shrink-0 rounded-full border border-line bg-soft px-2 py-1 text-xs font-semibold text-muted">
            {accessVisibility === 'shared' ? 'Shared' : 'Private'}
          </span>
        {/if}
      </div>
    {/if}
  {:else}
    <a class={cn('block w-full min-w-0 max-w-full text-inherit no-underline', focusRing)} {href} aria-label={`Open ${title}`} onclick={handleDetailClick}>
      <MemeMedia {meme} preview />
      {#if showTitle || (showAccessMarkers && accessVisibility !== 'public')}
        <div class="flex items-start justify-between gap-3 px-3 pb-3 pt-2.5">
          {#if showTitle}
            <p id={titleId} class="m-0 line-clamp-2 text-sm font-semibold leading-snug text-ink sm:text-base">{title}</p>
          {/if}
          {#if showAccessMarkers && accessVisibility !== 'public'}
            <span class="ml-auto shrink-0 rounded-full border border-line bg-soft px-2 py-1 text-xs font-semibold text-muted">
              {accessVisibility === 'shared' ? 'Shared' : 'Private'}
            </span>
          {/if}
        </div>
      {/if}
    </a>
  {/if}
  <MemeZoomDialog {meme} />
  <MemeActionMenu {meme} {href} {attribution} surface="card" />
</article>
