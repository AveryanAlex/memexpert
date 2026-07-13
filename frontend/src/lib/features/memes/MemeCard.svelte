<script lang="ts">
  import { browser } from '$app/environment';
  import { recordMemeDetailClick, recordMemeImpression } from '$lib/api/client';
  import type { MemeResultAttributionRead, PublicMemeCardRead } from '$lib/api/types';
  import { memeActionAttributionBody, memeHref, memeTitle } from '$lib/memeActions';
  import { cn, focusRing } from '$lib/ui/styles';
  import MemeActionMenu from './MemeActionMenu.svelte';
  import MemeMedia from './MemeMedia.svelte';

  interface Props {
    meme: PublicMemeCardRead;
    attribution?: MemeResultAttributionRead | null;
    position?: number;
    total?: number;
    showAccessMarkers?: boolean;
  }

  let { meme, attribution = null, position, total, showAccessMarkers = false }: Props = $props();
  let cardElement = $state<HTMLElement>();
  let recordedImpressionFor = $state<string | null>(null);

  const href = $derived(memeHref(meme, attribution));
  const title = $derived(memeTitle(meme));
  const showTitle = $derived(title !== 'Untitled meme');
  const titleId = $derived(`meme-card-title-${meme.id}`);
  const accessVisibility = $derived(meme.viewer_access?.visibility ?? 'public');
  const actionBody = $derived(memeActionAttributionBody(attribution));
  const telemetryRequest = $derived({ fetch, memeId: meme.id, body: actionBody, keepalive: true });

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
  class="w-full min-w-0 max-w-full overflow-hidden rounded-xl border border-line bg-paper"
  role={position ? 'listitem' : undefined}
  aria-posinset={position}
  aria-setsize={total}
  aria-labelledby={showTitle ? titleId : undefined}
>
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
  <MemeActionMenu {meme} {href} {attribution} surface="card" />
</article>
