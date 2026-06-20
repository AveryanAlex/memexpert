<script lang="ts">
  import { browser } from '$app/environment';
  import { fetchMemeOfTheDay } from '$lib/api/client';
  import type { PublicMemeOfTheDayRead } from '$lib/api/types';
  import { Badge, Button, Card, LoadingState, Notice } from '$lib/ui';
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

  function candidateLabel(count: number): string {
    return `${count} candidate${count === 1 ? '' : 's'}`;
  }
</script>

<Card class="my-6 overflow-hidden bg-gradient-to-br from-paper via-cream to-soft/60" aria-labelledby={headingId}>
  <div class="grid gap-5 lg:grid-cols-[minmax(0,0.9fr)_minmax(18rem,1.1fr)] lg:items-start">
    <div class="grid gap-3">
      <p class="m-0 text-sm font-extrabold uppercase tracking-[0.18em] text-muted">Daily pick</p>
      <h2 id={headingId} class="m-0 text-3xl font-black tracking-[-0.05em] text-ink md:text-4xl">Meme of the Day</h2>
      <p class="m-0 max-w-prose text-muted">A fresh public pick for the homepage, selected from eligible catalog memes.</p>

      {#if current}
        <div class="flex flex-wrap gap-2" aria-label="Meme of the Day metadata">
          <Badge>Selected {current.selected_for}</Badge>
          <Badge>{candidateLabel(current.candidate_count)}</Badge>
          {#if current.algorithm_version}
            <Badge>Algorithm {current.algorithm_version}</Badge>
          {/if}
        </div>
      {/if}
    </div>

    <div class="min-w-0">
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
          data-discovery-source={current.attribution?.source_algorithm ?? undefined}
          data-discovery-reason={current.attribution?.reason ?? undefined}
          data-discovery-request-id={current.attribution?.request_id ?? undefined}
          data-discovery-impression-id={current.attribution?.impression_id ?? undefined}
          data-discovery-score={current.attribution?.score ?? undefined}
        >
          <MemeCard meme={current.meme} attribution={current.attribution} {showAccessMarkers} />
        </div>
      {:else}
        <div class="grid gap-2 rounded-[28px] border border-line bg-paper/80 p-5" role="status">
          <p class="m-0 text-xl font-black tracking-[-0.04em] text-ink">No Meme of the Day yet</p>
          <p class="m-0 text-muted">The selector did not find an eligible public meme{current ? ` for ${current.selected_for}` : ''}. Check back after the next refresh.</p>
        </div>
      {/if}
    </div>
  </div>
</Card>
