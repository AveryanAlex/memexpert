<script lang="ts">
  import { browser } from '$app/environment';
  import { DEFAULT_PAGE_SIZE, fetchMemePage } from '$lib/api/client';
  import type { PublicMemeSearchPageRead } from '$lib/api/types';
  import { Button, EmptyState, LoadingState, Notice } from '$lib/ui';
  import type { Snippet } from 'svelte';
  import type { MemeGridBulkOptions } from './bulk-view-model';
  import {
    appendUniqueMemeResults,
    memeFeedKey,
    nextMemePageOffset,
    uniqueMemeResults,
    type MemeFeedFilters
  } from './infinite-feed';
  import MemeGrid from './MemeGrid.svelte';

  let {
    initialPage,
    filters,
    initialError = null,
    label = 'Meme results',
    emptyTitle = 'No memes found',
    emptyMessage = 'Try a shorter phrase, a different synonym, or clear the filters to browse.',
    bulk = { enabled: false },
    summary,
    emptyAction
  }: {
    initialPage: PublicMemeSearchPageRead;
    filters: MemeFeedFilters;
    initialError?: string | null;
    label?: string;
    emptyTitle?: string;
    emptyMessage?: string;
    bulk?: MemeGridBulkOptions;
    summary?: Snippet;
    emptyAction?: Snippet;
  } = $props();

  let loadedItems = $state<PublicMemeSearchPageRead['items'] | null>(null);
  let loadedTotal = $state<number | null>(null);
  let loadedLimit = $state<number | null>(null);
  let loadedNextOffset = $state<number | null>(null);
  let loadedHasMore = $state<boolean | null>(null);
  let loading = $state(false);
  let loadedErrorMessage = $state<string | null | undefined>(undefined);
  let observerAvailable = $state(false);
  let sentinel: HTMLDivElement | null = $state(null);
  let activeFeedKey = $state<string | null>(null);

  let activeController: AbortController | null = null;
  let loadToken = 0;

  const currentFeedKey = $derived(memeFeedKey(filters));
  const items = $derived(loadedItems ?? uniqueMemeResults(initialPage.items));
  const total = $derived(loadedTotal ?? initialPage.total);
  const limit = $derived(loadedLimit ?? (initialPage.limit || DEFAULT_PAGE_SIZE));
  const initialOffset = $derived(initialPage.offset);
  const nextOffset = $derived(loadedNextOffset ?? nextMemePageOffset(initialPage));
  const hasMore = $derived(loadedHasMore ?? initialPage.has_more);
  const errorMessage = $derived(loadedErrorMessage === undefined ? initialError : loadedErrorMessage);
  const memes = $derived(items.map((item) => item.meme));
  const firstLoading = $derived(loading && items.length === 0);
  const nextLoading = $derived(loading && items.length > 0);
  const showingCount = $derived(items.length);
  const showEmpty = $derived(!loading && !errorMessage && items.length === 0);
  const showEnd = $derived(!loading && !errorMessage && items.length > 0 && !hasMore);
  const showLoadMore = $derived(hasMore && items.length > 0);

  $effect(() => {
    if (currentFeedKey === activeFeedKey) return;

    activeFeedKey = currentFeedKey;
    loadToken += 1;
    activeController?.abort();
    activeController = null;
    loadedItems = null;
    loadedTotal = null;
    loadedLimit = null;
    loadedNextOffset = null;
    loadedHasMore = null;
    loadedErrorMessage = undefined;
    loading = false;
  });

  $effect(() => {
    if (!browser || !sentinel || !('IntersectionObserver' in window)) {
      observerAvailable = false;
      return;
    }

    observerAvailable = true;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          void loadNext();
        }
      },
      { rootMargin: '420px 0px' }
    );

    observer.observe(sentinel);

    return () => {
      observer.disconnect();
    };
  });

  async function loadNext() {
    if (!hasMore || loading || errorMessage || items.length === 0) return;
    await loadPage(nextOffset, 'append');
  }

  async function retry() {
    await loadPage(items.length === 0 ? initialOffset : nextOffset, items.length === 0 ? 'replace' : 'append');
  }

  async function loadPage(offset: number, mode: 'append' | 'replace') {
    if (!browser || loading) return;

    const token = loadToken + 1;
    loadToken = token;
    activeController?.abort();
    const controller = new AbortController();
    activeController = controller;
    loading = true;
    loadedErrorMessage = null;

    try {
      const page = await fetchMemePage({
        fetch: (input, init) => fetch(input, { ...init, signal: controller.signal }),
        baseUrl: window.location.origin,
        query: filters.query,
        tags: filters.tags,
        includeNsfw: filters.includeNsfw,
        mediaType: filters.mediaType,
        language: filters.language,
        limit,
        offset
      });

      if (token !== loadToken) return;

      loadedItems = mode === 'replace' ? uniqueMemeResults(page.items) : appendUniqueMemeResults(items, page.items);
      loadedTotal = page.total;
      loadedLimit = page.limit || limit;
      loadedNextOffset = nextMemePageOffset(page);
      loadedHasMore = page.has_more;
    } catch (error) {
      if (controller.signal.aborted || token !== loadToken) return;
      loadedErrorMessage = error instanceof Error ? error.message : 'Could not load more memes.';
    } finally {
      if (token === loadToken) {
        loading = false;
        activeController = null;
      }
    }
  }
</script>

<div class="my-7 flex flex-wrap items-center justify-between gap-3">
  <div class="flex flex-wrap items-center gap-2">
    {#if summary}{@render summary()}{/if}
  </div>
  <p class="m-0 text-muted">Showing {showingCount} of {total}</p>
</div>

{#if firstLoading}
  <LoadingState label="Loading meme results" />
{:else if errorMessage && items.length === 0}
  <Notice tone="danger" role="alert">{errorMessage}</Notice>
  <Button type="button" variant="secondary" onclick={retry} disabled={loading}>Retry</Button>
{:else if showEmpty}
  <EmptyState title={emptyTitle} message={emptyMessage}>
    {#if emptyAction}{@render emptyAction()}{/if}
  </EmptyState>
{:else}
  <MemeGrid {memes} {label} {bulk} />
{/if}

<div bind:this={sentinel} aria-hidden="true" class="h-1"></div>

<div class="mt-6 grid justify-items-center gap-3 text-center">
  {#if nextLoading}
    <LoadingState label="Loading more memes" />
  {/if}

  {#if errorMessage && items.length > 0}
    <Notice tone="danger" role="alert" class="w-full max-w-2xl">{errorMessage}</Notice>
    <Button type="button" variant="secondary" onclick={retry} disabled={loading}>Retry loading more</Button>
  {:else if showLoadMore}
    <Button type="button" variant="secondary" onclick={loadNext} disabled={loading}>Load more</Button>
    <p class="m-0 text-sm text-muted">
      {#if observerAvailable}
        More results also load automatically as you scroll.
      {:else}
        Automatic loading is unavailable in this browser, so use Load more.
      {/if}
    </p>
  {:else if showEnd}
    <p class="m-0 rounded-full border border-line bg-paper px-4 py-2 text-sm font-extrabold text-muted">End of results.</p>
  {/if}
</div>
