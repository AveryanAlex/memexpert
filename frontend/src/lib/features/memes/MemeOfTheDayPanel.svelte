<script lang="ts">
  import { browser } from '$app/environment';
  import { fetchMemeOfTheDay } from '$lib/api/client';
  import type { PublicMemeOfTheDayRead } from '$lib/api/types';
  import { Button, Card, LoadingState, Notice } from '$lib/ui';
  import { memeDiscoveryDataAttributes } from './discovery-attribution';
  import MemeCard from './MemeCard.svelte';

  interface Props {
    memeOfTheDay: PublicMemeOfTheDayRead | null;
    initialError?: string | null;
    showAccessMarkers?: boolean;
  }

  let { memeOfTheDay, initialError = null, showAccessMarkers = false }: Props = $props();
  let loadedMemeOfTheDay = $state<PublicMemeOfTheDayRead | null | undefined>(undefined);
  let loadedErrorMessage = $state<string | null | undefined>(undefined);
  let loading = $state(false);

  const headingId = 'meme-of-the-day-heading';
  const current = $derived(loadedMemeOfTheDay === undefined ? memeOfTheDay : loadedMemeOfTheDay);
  const errorMessage = $derived(loadedErrorMessage === undefined ? initialError : loadedErrorMessage);

  $effect(() => {
    memeOfTheDay;
    initialError;
    loadedMemeOfTheDay = undefined;
    loadedErrorMessage = undefined;
  });

  async function retry() {
    if (!browser || loading) return;

    loading = true;
    loadedErrorMessage = null;

    try {
      loadedMemeOfTheDay = await fetchMemeOfTheDay({
        fetch: (input, init) => fetch(input, init),
        baseUrl: window.location.origin
      });
    } catch (error) {
      loadedErrorMessage = error instanceof Error ? error.message : 'Could not load Meme of the Day.';
    } finally {
      loading = false;
    }
  }

</script>

<Card class="mb-5 overflow-hidden p-3 sm:p-4" aria-labelledby={headingId}>
  <div class="grid gap-4 md:grid-cols-[minmax(12rem,0.7fr)_minmax(20rem,1fr)] md:items-start">
    <div class="grid content-start gap-2 md:py-2">
      <div>
        <p class="m-0 text-xs font-semibold uppercase tracking-[0.16em] text-muted">Daily pick</p>
        <h2 id={headingId} class="m-0 text-xl font-black tracking-[-0.04em] text-ink sm:text-2xl">Meme of the Day</h2>
      </div>
      <p class="m-0 hidden max-w-sm text-sm leading-relaxed text-muted md:block">One standout from today’s catalog, ready to save or send.</p>
    </div>

    {#if loading}
      <LoadingState label="Loading Meme of the Day" />
    {:else if errorMessage}
      <div class="grid gap-3">
        <Notice tone="danger" role="alert" class="my-0">{errorMessage}</Notice>
        <div>
          <Button type="button" variant="secondary" onclick={retry} disabled={loading}>Retry</Button>
        </div>
      </div>
    {:else if current?.meme}
      <div
        class="w-full md:max-w-[34rem] md:justify-self-end"
        {...memeDiscoveryDataAttributes(current.attribution)}
      >
        <MemeCard
          meme={current.meme}
          attribution={current.attribution}
          exposureId={current.attribution?.impression_id}
          exposurePlacement={`meme-of-the-day:${current.meme.id}`}
          {showAccessMarkers}
          showZoom={true}
          videoPreviewMode="poster"
        />
      </div>
    {:else}
      <div class="grid gap-1 py-3" role="status">
        <p class="m-0 text-lg font-black tracking-[-0.04em] text-ink">No Meme of the Day yet</p>
        <p class="m-0 text-muted">Check back soon for a fresh pick.</p>
      </div>
    {/if}
  </div>
</Card>
