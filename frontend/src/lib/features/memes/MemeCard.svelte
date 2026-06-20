<script lang="ts">
  import { browser } from '$app/environment';
  import { recordMemeDetailClick, recordMemeImpression } from '$lib/api/client';
  import type { MemeResultAttributionRead, PublicMemeCardRead } from '$lib/api/types';
  import { memeActionAttributionBody, memeHref, memeTitle } from '$lib/memeActions';
  import Badge from '$lib/ui/Badge.svelte';
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
  class="relative grid min-h-[16.25rem] overflow-hidden rounded-[28px] border border-line bg-paper shadow-warm"
  role={position ? 'listitem' : undefined}
  aria-posinset={position}
  aria-setsize={total}
  aria-labelledby={titleId}
>
  <a class={cn('grid rounded-[28px] text-inherit no-underline', focusRing)} {href} aria-label={`Open ${title}`} onclick={handleDetailClick}>
    <MemeMedia {meme} preview />
    <div class="grid content-between gap-4 p-4">
      <p id={titleId} class="m-0 text-lg font-extrabold leading-tight">{title}</p>
      <div class="flex flex-wrap gap-2" aria-label="Meme metadata">
        <Badge>{meme.language}</Badge>
        <Badge>{meme.like_count} likes</Badge>
        {#if showAccessMarkers && accessVisibility !== 'public'}
          <Badge class="bg-paper/80 text-muted">{accessVisibility === 'shared' ? 'Shared' : 'Private'}</Badge>
        {/if}
        {#if meme.primary_file?.width && meme.primary_file.height}
          <Badge>{meme.primary_file.width}x{meme.primary_file.height}</Badge>
        {/if}
      </div>
      {#if meme.tags.length > 0}
        <div class="flex flex-wrap gap-2" aria-label="Tags">
          {#each meme.tags.slice(0, 3) as tag}
            <Badge>#{tag}</Badge>
          {/each}
        </div>
      {/if}
    </div>
  </a>
  <div class="absolute right-3 top-3 z-10">
    <MemeActionMenu {meme} {href} {attribution} compact />
  </div>
</article>
