<script lang="ts">
  import { navigating } from '$app/state';
  import { readAuthState } from '$lib/auth-state';
  import type { MemeSearchScope } from '$lib/api/types';
  import { bulkGuidanceFromSessionAndCollections, collectionListBulkOptions } from '$lib/features/memes/bulk-view-model';
  import InfiniteMemeFeed from '$lib/features/memes/InfiniteMemeFeed.svelte';
  import ActiveSearchFilters from '$lib/features/search/ActiveSearchFilters.svelte';
  import SearchFilters from '$lib/features/search/SearchFilters.svelte';
  import { ActionLink, LoadingState } from '$lib/ui';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  const authState = readAuthState(() => ({ session: data.session ?? null, sessionError: data.sessionError }));
  const session = $derived($authState.session);

  const bulkOptions = $derived(collectionListBulkOptions(data.collections));
  const bulkGuidance = $derived(bulkGuidanceFromSessionAndCollections(session, bulkOptions));
  const loadingSearch = $derived(navigating.to?.url.pathname === '/search');
  const hasActiveSearchFilters = $derived(
    data.filters.tags.length > 0 ||
      data.filters.includeNsfw ||
      Boolean(data.filters.mediaType) ||
      Boolean(data.filters.language) ||
      data.filters.scope !== 'public'
  );

  function browsingLabel(scope: MemeSearchScope): string {
    if (scope === 'collections') return 'selected collections';
    if (scope === 'private') return 'your saved memes';
    if (scope === 'all') return 'everywhere';
    return 'public memes';
  }
</script>

<svelte:head>
  <link rel="canonical" href={data.seo.canonicalUrl} />
  {#if data.seo.noindex}
    <meta name="robots" content="noindex,follow" />
  {/if}
</svelte:head>

<section class="mb-4 grid gap-3 border-b border-line pb-4 sm:grid-cols-[auto_minmax(0,1fr)] sm:items-center">
  <div>
    <p class="m-0 text-sm font-semibold text-muted">Search</p>
    <h1 class="m-0 text-3xl font-bold tracking-[-0.05em] text-ink">Find a meme</h1>
  </div>
  <SearchFilters
    filters={data.filters}
    {session}
    collections={data.collections}
    collectionErrorMessage={data.collectionErrorMessage}
  />
</section>

<ActiveSearchFilters filters={data.filters} collections={data.collections} />

{#if loadingSearch}
  <LoadingState label="Loading filtered results" />
{/if}

<InfiniteMemeFeed
  initialPage={data.page}
  filters={{
    query: data.filters.query,
    tags: data.filters.tags,
    includeNsfw: data.filters.includeNsfw,
    mediaType: data.filters.mediaType,
    language: data.filters.language,
    scope: data.filters.scope,
    collectionIds: data.filters.collectionIds
  }}
  initialError={data.errorMessage}
  label="Search results"
  layout="ordered"
  emptyMessage="Try a shorter phrase, remove a tag, or broaden media and language filters."
  bulk={{ enabled: true, saveEnabled: true, collectionOptions: bulkOptions, guidance: bulkGuidance }}
  showAccessMarkers={Boolean(session)}
>
  {#snippet summary()}
    {#if data.filters.query}
      <p class="m-0 text-muted">Results for “{data.filters.query}”</p>
    {:else}
      <p class="m-0 text-muted">Browsing {browsingLabel(data.filters.scope)}</p>
    {/if}
  {/snippet}
  {#snippet emptyAction()}
    {#if hasActiveSearchFilters || data.filters.query}
      <ActionLink href="/search">Browse everything</ActionLink>
    {/if}
  {/snippet}
</InfiniteMemeFeed>
