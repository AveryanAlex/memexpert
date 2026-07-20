<script lang="ts">
  import { browser } from '$app/environment';
  import { createAccumulatedForegroundTimer, hasEngagedViewVisibility } from '$lib/analytics/engaged-view';
  import { readMemeInteractionQueue } from '$lib/analytics/interaction-queue';
  import type { MemeResultAttributionRead, PublicMemeCardRead } from '$lib/api/types';
  import { selectMediaRender } from '$lib/media/render';
  import { memeHref, memeTitle } from '$lib/memeActions';
  import { cn, focusRing } from '$lib/ui/styles';
  import { ExternalLink } from '@lucide/svelte';
  import MemeActionMenu from './MemeActionMenu.svelte';
  import MemeMedia from './MemeMedia.svelte';
  import MemeZoomDialog from './MemeZoomDialog.svelte';
  import { hasQualifyingMemeExposure, readMemeExposureScope } from './meme-exposure-scope';
  import type { MemeVideoPreviewMode } from './meme-video';

  interface Props {
    meme: PublicMemeCardRead;
    attribution?: MemeResultAttributionRead | null;
    exposureId?: string;
    exposurePlacement?: string;
    position?: number;
    total?: number;
    showAccessMarkers?: boolean;
    showZoom?: boolean;
    videoPreviewMode?: MemeVideoPreviewMode;
  }

  let {
    meme,
    attribution = null,
    exposureId,
    exposurePlacement,
    position,
    total,
    showAccessMarkers = false,
    showZoom = true,
    videoPreviewMode = 'poster'
  }: Props = $props();
  let cardElement = $state<HTMLElement>();
  const componentId = $props.id();
  const exposureScope = readMemeExposureScope();
  const interactionQueue = readMemeInteractionQueue();

  const providedExposureId = $derived(firstNonBlankExposureId(exposureId, attribution?.impression_id));
  const resolvedExposureId = $derived.by(() => {
    const scopeState = $exposureScope;
    if (providedExposureId) {
      return exposureScope.resolveExposureId(providedExposureId, exposurePlacement ?? `card:${componentId}:${meme.id}`);
    }
    if (!scopeState.clientReady) return null;
    return exposureScope.resolveExposureId(
      null,
      exposurePlacement ?? `card:${componentId}:${meme.id}`
    );
  });
  const exposureAttribution = $derived(
    resolvedExposureId ? { ...(attribution ?? {}), impression_id: resolvedExposureId } : attribution
  );
  const href = $derived(memeHref(meme, exposureAttribution));
  const title = $derived(memeTitle(meme));
  const showTitle = $derived(title !== 'Untitled meme');
  const titleId = $derived(`meme-card-title-${meme.id}`);
  const accessVisibility = $derived(meme.viewer_access?.visibility ?? 'public');
  const attributionToken = $derived(attribution?.attribution_token ?? null);
  const isVideo = $derived(Boolean(selectMediaRender(meme.primary_file).videoUrl));

  $effect(() => {
    if (
      !browser ||
      !$exposureScope.clientReady ||
      !resolvedExposureId ||
      !cardElement ||
      (exposureScope.hasRecorded(resolvedExposureId) && exposureScope.hasRecorded(`${resolvedExposureId}:engaged`)) ||
      typeof IntersectionObserver === 'undefined'
    ) return;

    const observedExposureId = resolvedExposureId;
    const engagedExposureId = `${observedExposureId}:engaged`;
    const engagementTimer = createAccumulatedForegroundTimer({
      onComplete: () => {
        if (!exposureScope.claim(engagedExposureId)) return;
        interactionQueue.enqueue({
          eventType: 'meme_engaged_view',
          memeId: meme.id,
          attributionToken,
          properties: { impression_id: observedExposureId }
        });
      }
    });
    const syncDocumentVisibility = () => {
      engagementTimer.setForeground(document.visibilityState !== 'hidden');
    };
    syncDocumentVisibility();
    document.addEventListener('visibilitychange', syncDocumentVisibility);

    const observer = new IntersectionObserver(
      (entries) => {
        const entry = [...entries].reverse().find((candidate) => candidate.target === cardElement);
        if (!entry) return;

        if (!exposureScope.hasRecorded(observedExposureId) && hasQualifyingMemeExposure([entry])) {
          if (exposureScope.claim(observedExposureId)) {
            interactionQueue.enqueue({
              eventType: 'meme_impression',
              memeId: meme.id,
              attributionToken,
              properties: { impression_id: observedExposureId }
            });
          }
        }
        engagementTimer.setVisible(hasEngagedViewVisibility(entry));
      },
      { threshold: [0.25, 0.5] }
    );
    observer.observe(cardElement);

    return () => {
      observer.disconnect();
      engagementTimer.dispose();
      document.removeEventListener('visibilitychange', syncDocumentVisibility);
    };
  });

  function handleDetailClick() {
    if (!browser) return;

    interactionQueue.enqueue({
      eventType: 'meme_detail_click',
      memeId: meme.id,
      attributionToken,
      properties: resolvedExposureId ? { impression_id: resolvedExposureId } : undefined
    });
  }

  function firstNonBlankExposureId(...values: Array<string | null | undefined>): string | null {
    return values.find((value): value is string => Boolean(value?.trim())) ?? null;
  }

</script>

<article
  bind:this={cardElement}
  class="relative w-full min-w-0 max-w-full overflow-hidden rounded-xl border border-line bg-paper"
  role={position ? 'listitem' : undefined}
  aria-posinset={position}
  aria-setsize={total}
  aria-labelledby={showTitle ? titleId : undefined}
  data-exposure-id={resolvedExposureId ?? undefined}
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
        data-sveltekit-preload-data="tap"
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
            data-sveltekit-preload-data="tap"
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
    <a
      class={cn('block w-full min-w-0 max-w-full text-inherit no-underline', focusRing)}
      {href}
      aria-label={`Open ${title}`}
      data-sveltekit-preload-data="tap"
      onclick={handleDetailClick}
    >
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
  <MemeZoomDialog {meme} showTrigger={showZoom} />
  <MemeActionMenu {meme} {href} attribution={exposureAttribution} surface="card" />
</article>
