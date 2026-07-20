<script lang="ts">
  import { browser } from '$app/environment';
  import { beforeNavigate } from '$app/navigation';
  import { DEFAULT_PAGE_SIZE, fetchHomeFeed, fetchMemePage, fetchSimilarMemes } from '$lib/api/client';
  import type { PublicMemeSearchPageRead } from '$lib/api/types';
  import { Button, EmptyState, LoadingState, Notice } from '$lib/ui';
  import { onDestroy, type Snippet } from 'svelte';
  import type { MemeGridBulkOptions } from './bulk-view-model';
  import {
    appendUniqueMemeResults,
    canLoadNextMemePage,
    INFINITE_FEED_OBSERVER_ROOT_MARGIN,
    memeFeedKey,
    nextMemePageOffset,
    uniqueMemeResults,
    type MemeFeedFilters,
    type MemeFeedSource
  } from './infinite-feed';
  import MemeGrid from './MemeGrid.svelte';

  let {
    initialPage,
    filters,
    initialError = null,
    retainInitialState = false,
    source = 'catalog',
    sourceMemeId = null,
    label = 'Meme results',
    emptyTitle = 'No memes found',
    emptyMessage = 'Try a shorter phrase, a different synonym, or clear the filters to browse.',
    bulk = { enabled: false },
    showAccessMarkers = false,
    summary,
    emptyAction
  }: {
    initialPage: PublicMemeSearchPageRead;
    filters: MemeFeedFilters;
    initialError?: string | null;
    retainInitialState?: boolean;
    source?: MemeFeedSource;
    sourceMemeId?: string | null;
    label?: string;
    emptyTitle?: string;
    emptyMessage?: string;
    bulk?: MemeGridBulkOptions;
    showAccessMarkers?: boolean;
    summary?: Snippet;
    emptyAction?: Snippet;
  } = $props();

  let capturedInitialPage = $state<PublicMemeSearchPageRead | null>(null);
  let capturedInitialError = $state<string | null | undefined>(undefined);
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
  let observedInitialPage: PublicMemeSearchPageRead | null = null;

  let activeController: AbortController | null = null;
  let activeLoadPreviousError: string | null = null;
  let errorRetryMode = $state<'append' | 'replace' | null>(null);
  let errorRetryOffset = $state<number | null>(null);
  let loadToken = 0;

  const currentFeedKey = $derived(memeFeedKey(filters, source, sourceMemeId));
  const basePage = $derived(capturedInitialPage ?? initialPage);
  const baseError = $derived(capturedInitialError === undefined ? initialError : capturedInitialError);
  const items = $derived(loadedItems ?? uniqueMemeResults(basePage.items));
  const total = $derived(loadedTotal ?? basePage.total);
  const limit = $derived(loadedLimit ?? (basePage.limit || DEFAULT_PAGE_SIZE));
  const initialOffset = $derived(basePage.offset);
  const nextOffset = $derived(loadedNextOffset ?? nextMemePageOffset(basePage));
  const hasMore = $derived(loadedHasMore ?? basePage.has_more);
  const errorMessage = $derived(loadedErrorMessage === undefined ? baseError : loadedErrorMessage);
  const memes = $derived(items.map((item) => item.meme));
  const attributions = $derived(Object.fromEntries(items.map((item) => [item.meme.id, item.attribution])));
  const firstLoading = $derived(loading && items.length === 0);
  const nextLoading = $derived(loading && items.length > 0);
  const showingCount = $derived(items.length);
  const showEmpty = $derived(!loading && !errorMessage && items.length === 0);
  const showEnd = $derived(!loading && !errorMessage && items.length > 0 && !hasMore);
  const showLoadMore = $derived(hasMore && items.length > 0);

  beforeNavigate(cancelActiveLoad);
  onDestroy(cancelActiveLoad);

  $effect(() => {
    if (currentFeedKey !== activeFeedKey) {
      activeFeedKey = currentFeedKey;
      observedInitialPage = initialPage;
      cancelActiveLoad();
      capturedInitialPage = initialPage;
      capturedInitialError = initialError;
      clearLoadedState();
      return;
    }

    if (observedInitialPage === initialPage) return;
    observedInitialPage = initialPage;
    if (retainInitialState) return;

    cancelActiveLoad();
    if (initialError && items.length > 0) {
      loadedErrorMessage = initialError;
      errorRetryMode = 'replace';
      errorRetryOffset = initialPage.offset;
      return;
    }

    capturedInitialPage = initialPage;
    capturedInitialError = initialError;
    clearLoadedState();
  });

  function clearLoadedState() {
    loadedItems = null;
    loadedTotal = null;
    loadedLimit = null;
    loadedNextOffset = null;
    loadedHasMore = null;
    loadedErrorMessage = undefined;
    errorRetryMode = null;
    errorRetryOffset = null;
  }

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
      { rootMargin: INFINITE_FEED_OBSERVER_ROOT_MARGIN }
    );

    observer.observe(sentinel);

    return () => {
      observer.disconnect();
    };
  });

  async function loadNext() {
    if (!canLoadNextMemePage({ hasMore, loading, errorMessage, itemCount: items.length })) return;
    await loadPage(nextOffset, 'append');
  }

  function cancelActiveLoad() {
    const hadActiveLoad = activeController !== null;
    loadToken += 1;
    activeController?.abort();
    activeController = null;
    if (hadActiveLoad) loadedErrorMessage = activeLoadPreviousError;
    activeLoadPreviousError = null;
    loading = false;
  }

  async function retry() {
    const mode = errorRetryMode ?? (items.length === 0 ? 'replace' : 'append');
    const offset = errorRetryOffset ?? (mode === 'replace' ? initialOffset : nextOffset);
    await loadPage(offset, mode);
  }

  async function loadPage(offset: number, mode: 'append' | 'replace') {
    if (!browser || loading) return;

    const token = loadToken + 1;
    loadToken = token;
    activeController?.abort();
    const controller = new AbortController();
    activeController = controller;
    loading = true;
    activeLoadPreviousError = errorMessage ?? null;
    loadedErrorMessage = null;

    try {
      const fetchRequest = {
        fetch: (input: RequestInfo | URL, init?: RequestInit) => fetch(input, { ...init, signal: controller.signal }),
        baseUrl: window.location.origin,
        tags: filters.tags,
        includeNsfw: filters.includeNsfw,
        mediaType: filters.mediaType,
        language: filters.language,
        limit,
        offset
      };
      let page: PublicMemeSearchPageRead;
      if (source === 'similar') {
        if (!sourceMemeId) {
          throw new Error('Could not identify the source meme for similar results.');
        }
        page = await fetchSimilarMemes({
          fetch: fetchRequest.fetch,
          baseUrl: fetchRequest.baseUrl,
          memeId: sourceMemeId,
          limit,
          offset
        });
      } else if (source === 'home' && !filters.query.trim()) {
        page = await fetchHomeFeed(fetchRequest);
      } else {
        page = await fetchMemePage({
          ...fetchRequest,
          query: filters.query,
          scope: filters.scope,
          collectionIds: filters.collectionIds
        });
      }

      if (token !== loadToken) return;

      loadedItems = mode === 'replace' ? uniqueMemeResults(page.items) : appendUniqueMemeResults(items, page.items);
      loadedTotal = page.total;
      loadedLimit = page.limit || limit;
      loadedNextOffset = nextMemePageOffset(page);
      loadedHasMore = page.has_more;
      errorRetryMode = null;
      errorRetryOffset = null;
    } catch (error) {
      if (controller.signal.aborted || token !== loadToken) return;
      loadedErrorMessage = error instanceof Error ? error.message : 'Could not load more memes.';
      errorRetryMode = mode;
      errorRetryOffset = offset;
    } finally {
      if (token === loadToken) {
        loading = false;
        activeController = null;
        activeLoadPreviousError = null;
      }
    }
  }
</script>

<div class="mb-4 flex flex-wrap items-center justify-between gap-3">
  <div class="flex flex-wrap items-center gap-2">
    {#if summary}{@render summary()}{/if}
  </div>
  <p class="m-0 text-muted" aria-live="polite">Showing {showingCount} of {total}</p>
</div>

{#if firstLoading}
  <LoadingState label="Loading meme results" />
{:else if errorMessage && items.length === 0}
  <Notice tone="danger" role="alert">{errorMessage}</Notice>
  <Button type="button" variant="secondary" onclick={() => void retry()} disabled={loading}>Retry</Button>
{:else if showEmpty}
  <EmptyState title={emptyTitle} message={emptyMessage}>
    {#if emptyAction}{@render emptyAction()}{/if}
  </EmptyState>
{:else}
  <MemeGrid {memes} {total} {label} {attributions} {bulk} {showAccessMarkers} />
{/if}

<div bind:this={sentinel} aria-hidden="true" class="h-1" data-infinite-feed-sentinel></div>

<div class="mt-5 grid justify-items-center gap-2 text-center">
  {#if nextLoading}
    <LoadingState label={errorRetryMode === 'replace' ? 'Refreshing meme results' : 'Loading more memes'} />
  {/if}

  {#if errorMessage && items.length > 0}
    <Notice tone="danger" role="alert" class="w-full max-w-2xl">{errorMessage}</Notice>
    <Button type="button" variant="secondary" onclick={() => void retry()} disabled={loading}>
      {errorRetryMode === 'replace' ? 'Retry refreshing results' : 'Retry loading more'}
    </Button>
  {:else if showLoadMore}
    <Button type="button" variant="secondary" onclick={() => void loadNext()} disabled={loading} aria-describedby="meme-feed-load-more-help">Load more</Button>
    <span id="meme-feed-load-more-help" class="sr-only">
      {#if observerAvailable}
        More results also load automatically as you scroll.
      {:else}
        Automatic loading is unavailable in this browser, so use Load more.
      {/if}
    </span>
  {:else if showEnd}
    <p class="m-0 text-sm font-semibold text-muted">You’re all caught up.</p>
  {/if}
</div>
