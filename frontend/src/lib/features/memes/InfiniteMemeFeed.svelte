<script lang="ts">
  import { browser } from '$app/environment';
  import { beforeNavigate } from '$app/navigation';
  import {
    DEFAULT_PAGE_SIZE,
    fetchHomeFeed,
    fetchMemePage,
    fetchSimilarMemes,
    reauthorizeHomeFeedItems
  } from '$lib/api/client';
  import type { PublicMemeSearchPageRead, RecommendationFeedPageRead } from '$lib/api/types';
  import { Button, EmptyState, LoadingState, Notice } from '$lib/ui';
  import { onDestroy, tick, type Snippet } from 'svelte';
  import type { MemeGridBulkOptions } from './bulk-view-model';
  import {
    clearRestorableHomeFeed,
    homeFeedStorageKey,
    loadHomeFeedWithCursorRecovery,
    loadRestorableHomeFeed,
    persistRestorableHomeFeed
  } from './home-feed-session';
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
    viewerId = null,
    initialPageViewerId = null,
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
    viewerId?: string | null;
    initialPageViewerId?: string | null;
    summary?: Snippet;
    emptyAction?: Snippet;
  } = $props();

  let capturedInitialPage = $state<PublicMemeSearchPageRead | null>(null);
  let capturedInitialError = $state<string | null | undefined>(undefined);
  let loadedItems = $state<PublicMemeSearchPageRead['items'] | null>(null);
  let loadedTotal = $state<number | null>(null);
  let loadedLimit = $state<number | null>(null);
  let loadedNextOffset = $state<number | null>(null);
  let loadedNextCursor = $state<string | null | undefined>(undefined);
  let loadedFeedSessionId = $state<string | null | undefined>(undefined);
  let loadedExpiresAt = $state<string | null | undefined>(undefined);
  let loadedHasMore = $state<boolean | null>(null);
  let loading = $state(false);
  let hydrated = $state(false);
  let loadedErrorMessage = $state<string | null | undefined>(undefined);
  let observerAvailable = $state(false);
  let sentinel: HTMLDivElement | null = $state(null);
  let activeFeedKey = $state<string | null>(null);
  let activeHomeStorageKey: string | null = null;
  let homeStateReady = $state(false);
  let observedInitialPage: PublicMemeSearchPageRead | null = null;
  let observedInitialPageViewerId: string | null = null;
  let homeRestoreController: AbortController | null = null;
  let homeRestoreToken = 0;

  let activeController: AbortController | null = null;
  let activeLoadPreviousError: string | null = null;
  let errorRetryMode = $state<'append' | 'replace' | null>(null);
  let errorRetryOffset = $state<number | null>(null);
  let errorRetryCursor = $state<string | null | undefined>(undefined);
  let loadToken = 0;

  const currentFeedKey = $derived(memeFeedKey(filters, source, sourceMemeId));
  const currentFeedStateKey = $derived(
    source === 'home' ? `${currentFeedKey}:viewer:${viewerId ?? 'anonymous'}` : currentFeedKey
  );
  const currentHomeStorageKey = $derived(homeFeedStorageKey(viewerId, currentFeedKey));
  const homeViewerMismatch = $derived(source === 'home' && initialPageViewerId !== viewerId);
  const basePage = $derived(capturedInitialPage ?? initialPage);
  const baseError = $derived(capturedInitialError === undefined ? initialError : capturedInitialError);
  const items = $derived(homeViewerMismatch ? [] : (loadedItems ?? uniqueMemeResults(basePage.items)));
  const total = $derived(homeViewerMismatch ? 0 : (loadedTotal ?? basePage.total));
  const limit = $derived(loadedLimit ?? (basePage.limit || DEFAULT_PAGE_SIZE));
  const initialOffset = $derived(basePage.offset);
  const nextOffset = $derived(loadedNextOffset ?? nextMemePageOffset(basePage));
  const nextCursor = $derived(
    loadedNextCursor === undefined
      ? isRecommendationFeedPage(basePage)
        ? basePage.next_cursor
        : null
      : loadedNextCursor
  );
  const feedSessionId = $derived(
    loadedFeedSessionId === undefined
      ? isRecommendationFeedPage(basePage)
        ? basePage.feed_session_id
        : null
      : loadedFeedSessionId
  );
  const expiresAt = $derived(
    loadedExpiresAt === undefined
      ? isRecommendationFeedPage(basePage)
        ? basePage.expires_at
        : null
      : loadedExpiresAt
  );
  const hasMore = $derived(
    !homeViewerMismatch && (loadedHasMore ?? basePage.has_more) && (source !== 'home' || Boolean(nextCursor))
  );
  const errorMessage = $derived(
    homeViewerMismatch ? null : (loadedErrorMessage === undefined ? baseError : loadedErrorMessage)
  );
  const memes = $derived(items.map((item) => item.meme));
  const attributions = $derived(Object.fromEntries(items.map((item) => [item.meme.id, item.attribution])));
  const firstLoading = $derived(loading && items.length === 0);
  const nextLoading = $derived(loading && items.length > 0);
  const showingCount = $derived(items.length);
  const showEmpty = $derived(!loading && !errorMessage && items.length === 0);
  const showEnd = $derived(!loading && !errorMessage && items.length > 0 && !hasMore);
  const showLoadMore = $derived(hasMore && items.length > 0);

  beforeNavigate(() => {
    persistCurrentHomeState();
    cancelActiveLoad();
  });
  onDestroy(cancelActiveLoad);

  $effect(() => {
    hydrated = true;
  });

  $effect(() => {
    if (currentFeedStateKey !== activeFeedKey) {
      if (browser && activeHomeStorageKey && activeHomeStorageKey !== currentHomeStorageKey) {
        clearRestorableHomeFeed(sessionStorage, activeHomeStorageKey);
      }
      activeFeedKey = currentFeedStateKey;
      activeHomeStorageKey = source === 'home' ? currentHomeStorageKey : null;
      homeStateReady = false;
      observedInitialPage = initialPage;
      observedInitialPageViewerId = initialPageViewerId;
      cancelActiveLoad();
      capturedInitialPage = homeViewerMismatch ? null : initialPage;
      capturedInitialError = initialError;
      clearLoadedState();
      if (!homeViewerMismatch) void restoreHomeState();
      return;
    }

    if (observedInitialPage === initialPage && observedInitialPageViewerId === initialPageViewerId) return;
    observedInitialPage = initialPage;
    observedInitialPageViewerId = initialPageViewerId;
    if (retainInitialState) return;

    cancelActiveLoad();
    if (homeViewerMismatch) {
      capturedInitialPage = null;
      capturedInitialError = undefined;
      clearLoadedState();
      homeStateReady = false;
      return;
    }
    if (initialError && items.length > 0) {
      loadedErrorMessage = initialError;
      errorRetryMode = 'replace';
      errorRetryOffset = initialPage.offset;
      return;
    }

    capturedInitialPage = initialPage;
    capturedInitialError = initialError;
    clearLoadedState();
    void restoreHomeState();
  });

  $effect(() => {
    if (
      !browser ||
      source !== 'home' ||
      homeViewerMismatch ||
      !homeStateReady ||
      !feedSessionId ||
      !expiresAt
    ) return;

    persistCurrentHomeState();
  });

  function persistCurrentHomeState() {
    if (
      !browser ||
      source !== 'home' ||
      homeViewerMismatch ||
      !homeStateReady ||
      !feedSessionId ||
      !expiresAt
    ) return;

    persistRestorableHomeFeed(sessionStorage, currentHomeStorageKey, {
      feedKey: currentFeedKey,
      viewerId,
      feedSessionId,
      expiresAt,
      items,
      total,
      limit,
      offset: 0,
      nextCursor,
      hasMore,
      scrollY: Math.max(0, window.scrollY)
    });
  }

  function clearLoadedState() {
    loadedItems = null;
    loadedTotal = null;
    loadedLimit = null;
    loadedNextOffset = null;
    loadedNextCursor = undefined;
    loadedFeedSessionId = undefined;
    loadedExpiresAt = undefined;
    loadedHasMore = null;
    loadedErrorMessage = undefined;
    errorRetryMode = null;
    errorRetryOffset = null;
    errorRetryCursor = undefined;
  }

  async function restoreHomeState() {
    if (!browser || source !== 'home' || basePage.offset > 0) {
      homeStateReady = true;
      return;
    }

    const restored = loadRestorableHomeFeed(
      sessionStorage,
      currentHomeStorageKey,
      { feedKey: currentFeedKey, viewerId }
    );
    if (!restored) {
      homeStateReady = true;
      return;
    }

    const restorableItems = restored.items.flatMap((item) => {
      const attributionToken = item.attribution.attribution_token?.trim();
      return attributionToken ? [{ meme_id: item.meme.id, attribution_token: attributionToken }] : [];
    });
    if (restorableItems.length !== restored.items.length) {
      clearRestorableHomeFeed(sessionStorage, currentHomeStorageKey);
      homeStateReady = true;
      return;
    }

    const token = homeRestoreToken + 1;
    homeRestoreToken = token;
    homeRestoreController?.abort();
    const controller = new AbortController();
    homeRestoreController = controller;
    try {
      const reauthorized = await reauthorizeHomeFeedItems({
        fetch: (input, init) => fetch(input, { ...init, signal: controller.signal }),
        baseUrl: window.location.origin,
        tags: filters.tags,
        includeNsfw: filters.includeNsfw,
        mediaType: filters.mediaType,
        language: filters.language,
        items: restorableItems
      });
      if (controller.signal.aborted || token !== homeRestoreToken || homeViewerMismatch) return;

      loadedItems = uniqueMemeResults(reauthorized.items);
      loadedTotal = restored.total;
      loadedLimit = restored.limit;
      loadedNextOffset = restored.offset;
      loadedNextCursor = restored.nextCursor;
      loadedFeedSessionId = restored.feedSessionId;
      loadedExpiresAt = restored.expiresAt;
      loadedHasMore = restored.hasMore;
      loadedErrorMessage = null;
      await restoreSavedScrollPosition(restored.scrollY, token, controller.signal);
    } catch {
      if (!controller.signal.aborted && token === homeRestoreToken) {
        clearRestorableHomeFeed(sessionStorage, currentHomeStorageKey);
      }
    } finally {
      if (token === homeRestoreToken) {
        homeRestoreController = null;
        homeStateReady = true;
      }
    }
  }

  async function restoreSavedScrollPosition(scrollY: number, token: number, signal: AbortSignal) {
    if (scrollY <= 0) return;

    await tick();
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    if (signal.aborted || token !== homeRestoreToken || homeViewerMismatch) return;
    window.scrollTo(0, scrollY);
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
    await loadPage({ offset: nextOffset, cursor: source === 'home' ? nextCursor : null }, 'append');
  }

  function cancelActiveLoad() {
    const hadActiveLoad = activeController !== null;
    loadToken += 1;
    activeController?.abort();
    activeController = null;
    homeRestoreToken += 1;
    homeRestoreController?.abort();
    homeRestoreController = null;
    if (hadActiveLoad) loadedErrorMessage = activeLoadPreviousError;
    activeLoadPreviousError = null;
    loading = false;
  }

  async function retry() {
    const mode = errorRetryMode ?? (items.length === 0 ? 'replace' : 'append');
    const offset = errorRetryOffset ?? (mode === 'replace' ? initialOffset : nextOffset);
    const cursor = errorRetryCursor === undefined ? (mode === 'append' && source === 'home' ? nextCursor : null) : errorRetryCursor;
    await loadPage({ offset, cursor }, mode);
  }

  async function loadPage(
    pagination: { offset: number; cursor: string | null },
    mode: 'append' | 'replace'
  ) {
    if (!browser || loading) return;

    const token = loadToken + 1;
    loadToken = token;
    activeController?.abort();
    const controller = new AbortController();
    activeController = controller;
    loading = true;
    activeLoadPreviousError = errorMessage ?? null;
    loadedErrorMessage = null;
    let effectiveMode = mode;
    let requestedOffset = pagination.offset;
    let requestedCursor = pagination.cursor;

    try {
      const fetchRequest = {
        fetch: (input: RequestInfo | URL, init?: RequestInit) => fetch(input, { ...init, signal: controller.signal }),
        baseUrl: window.location.origin,
        tags: filters.tags,
        includeNsfw: filters.includeNsfw,
        mediaType: filters.mediaType,
        language: filters.language,
        limit,
        offset: requestedOffset
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
          offset: requestedOffset
        });
      } else if (source === 'home' && !filters.query.trim()) {
        const homeResult = await loadHomeFeedWithCursorRecovery({
          cursor: requestedCursor,
          load: (cursor) => fetchHomeFeed({
            ...fetchRequest,
            offset: cursor ? undefined : requestedCursor ? 0 : requestedOffset,
            cursor
          }),
          onExpired: () => {
            clearRestorableHomeFeed(sessionStorage, currentHomeStorageKey);
            requestedCursor = null;
            requestedOffset = 0;
            effectiveMode = 'replace';
          }
        });
        page = homeResult.page;
      } else {
        page = await fetchMemePage({
          ...fetchRequest,
          query: filters.query,
          scope: filters.scope,
          collectionIds: filters.collectionIds
        });
      }

      if (token !== loadToken) return;

      loadedItems = effectiveMode === 'replace' ? uniqueMemeResults(page.items) : appendUniqueMemeResults(items, page.items);
      loadedTotal = page.total;
      loadedLimit = page.limit || limit;
      loadedNextOffset = nextMemePageOffset(page);
      if (source === 'home' && isRecommendationFeedPage(page)) {
        loadedNextCursor = page.next_cursor;
        loadedFeedSessionId = page.feed_session_id;
        loadedExpiresAt = page.expires_at;
        loadedHasMore = page.has_more && Boolean(page.next_cursor);
      } else {
        loadedHasMore = page.has_more;
      }
      errorRetryMode = null;
      errorRetryOffset = null;
      errorRetryCursor = undefined;
    } catch (error) {
      if (controller.signal.aborted || token !== loadToken) return;
      loadedErrorMessage = error instanceof Error ? error.message : 'Could not load more memes.';
      errorRetryMode = effectiveMode;
      errorRetryOffset = requestedOffset;
      errorRetryCursor = requestedCursor;
    } finally {
      if (token === loadToken) {
        loading = false;
        activeController = null;
        activeLoadPreviousError = null;
      }
    }
  }

  function isRecommendationFeedPage(page: PublicMemeSearchPageRead): page is RecommendationFeedPageRead {
    const candidate = page as Partial<RecommendationFeedPageRead>;
    return (
      typeof candidate.feed_session_id === 'string' &&
      (candidate.next_cursor === null || typeof candidate.next_cursor === 'string') &&
      typeof candidate.expires_at === 'string'
    );
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
    <Button type="button" variant="secondary" onclick={() => void loadNext()} disabled={!hydrated || loading} aria-describedby="meme-feed-load-more-help">Load more</Button>
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
