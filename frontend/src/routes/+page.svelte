<script lang="ts">
  import CollectionChip from '$lib/features/collections/CollectionChip.svelte';
  import { bulkGuidanceFromSessionAndCollections, collectionListBulkOptions } from '$lib/features/memes/bulk-view-model';
  import InfiniteMemeFeed from '$lib/features/memes/InfiniteMemeFeed.svelte';
  import type { MemeFeedSource } from '$lib/features/memes/infinite-feed';
  import { ActionLink, Button, Card, Input, Notice, PageHeader, Select } from '$lib/ui';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

  const bulkOptions = $derived(collectionListBulkOptions(data.collections));
  const bulkGuidance = $derived(bulkGuidanceFromSessionAndCollections(data.session ?? null, bulkOptions));
  const feedSource = $derived(toMemeFeedSource(data.feedSource));
  const isHomeFeed = $derived(feedSource === 'home' && !data.query.trim());
  const homeFeedCopy = $derived(homeFeedSummary(data));

  function toMemeFeedSource(value: PageData['feedSource']): MemeFeedSource {
    return value === 'home' ? 'home' : 'catalog';
  }

  function homeFeedSummary(pageData: PageData): { title: string; message: string } {
    const attribution = pageData.page.items[0]?.attribution;
    const source = attribution?.source_algorithm;
    const reason = attribution?.reason ?? '';
    const isFull = pageData.session?.user.account_type === 'full';

    if (source === 'personalized_recommendations') {
      return {
        title: 'Personalized for you',
        message: 'Based on your recent MemeXpert likes, saves, sends, and detail views.'
      };
    }

    if (source === 'fallback_trending' && reason.startsWith('cold_start')) {
      return isFull
        ? {
            title: 'Trending while we learn your taste',
            message: 'Like, save, or open memes to turn this cold-start feed into personal recommendations.'
          }
        : {
            title: 'Trending for guests',
            message: 'A cold-start feed from public activity while this guest session has little history.'
          };
    }

    if (source === 'fallback_trending') {
      return {
        title: 'Trending fallback',
        message: 'Recommendations are temporarily degraded, so the backend is serving trending memes.'
      };
    }

    return {
      title: isFull ? 'Recommended home feed' : 'Guest home feed',
      message: isFull ? 'Your home feed updates as you interact with memes.' : 'Browse trending public memes, then connect Telegram when you want a full profile.'
    };
  }
</script>

<PageHeader title="Find the right meme fast." description="Search the public MemeXpert catalog with plain text, or browse a home feed that adapts as you use it." badge="Guest access enabled" />

<form class="mb-6 flex flex-col gap-2 rounded-3xl border border-line bg-paper p-2 shadow-warm-lg md:flex-row" method="GET" action="/search">
  <Input
    class="flex-1 border-0 bg-transparent"
    aria-label="Search memes"
    name="q"
    type="search"
    placeholder="try: cat reaction, distracted boyfriend, friday mood"
    value={data.query}
  />
  <Button type="submit">Search</Button>
</form>

<div class="mb-6 flex flex-wrap gap-2">
  <ActionLink variant="secondary" size="compact" href="/search">Advanced search</ActionLink>
  <ActionLink variant="ghost" size="compact" href="/search?tags=reaction&include_nsfw=false">Browse reactions</ActionLink>
</div>

<Card class="my-6 grid gap-4" aria-labelledby="collections-title">
  <div>
    <h2 id="collections-title" class="m-0 text-2xl font-black tracking-[-0.04em]">Your collections</h2>
    <p class="m-0 text-muted">Use Favorites for quick saves, or create custom collections from a full account.</p>
  </div>

  {#if data.collections}
    <div class="flex flex-wrap gap-2" aria-label="Collection list">
      {#each data.collections.collections as item (item.collection.id)}
        <CollectionChip
          href={`/collection/${item.collection.id}`}
          title={item.collection.title}
          meta={`${item.collection.kind}${item.active_save_collection_id === item.collection.id ? ' · active' : ''}`}
          active={item.active_save_collection_id === item.collection.id}
        />
      {/each}
    </div>
  {:else}
    <p class="m-0 text-muted">Collections are unavailable until your session is ready.</p>
  {/if}

  {#if form?.collectionError}
    <Notice role="alert" tone="danger">{form.collectionError}</Notice>
  {:else if form?.successMessage}
    <Notice>
      {form.successMessage}
      {#if form.collectionCreatedId}
        <a href={`/collection/${form.collectionCreatedId}`}>Open it</a>
      {/if}
    </Notice>
  {/if}

  {#if data.session?.user.account_type === 'full'}
    <form class="flex flex-wrap items-stretch gap-2" method="POST" action="?/createCollection">
      <Input name="title" placeholder="New collection title" maxlength={120} required aria-label="New collection title" />
      <Input name="description" placeholder="Description" aria-label="Collection description" />
      <Select name="visibility" aria-label="Collection visibility">
        <option value="private">Private</option>
        <option value="unlisted">Unlisted</option>
      </Select>
      <Button type="submit">Create collection</Button>
    </form>
  {:else}
    <p class="m-0 text-muted">Connect Telegram to create custom collections and collaborate.</p>
  {/if}
</Card>

<InfiniteMemeFeed
  initialPage={data.page}
  filters={{ query: data.query }}
  source={feedSource}
  initialError={data.errorMessage}
  emptyTitle={isHomeFeed ? 'No home feed memes yet' : 'No memes found'}
  emptyMessage={isHomeFeed ? 'Try Search or check back after the public catalog has more memes.' : 'Try a shorter phrase, a different synonym, or clear the search box to browse.'}
  bulk={{ enabled: true, saveEnabled: true, collectionOptions: bulkOptions, guidance: bulkGuidance }}
>
  {#snippet summary()}
    {#if data.query}
      <p class="m-0 text-muted">
        Results for “{data.query}”
      </p>
    {:else}
      <div class="grid gap-1">
        <p class="m-0 font-extrabold text-ink">{homeFeedCopy.title}</p>
        <p class="m-0 text-sm text-muted">{homeFeedCopy.message}</p>
      </div>
    {/if}
  {/snippet}
  {#snippet emptyAction()}
    {#if isHomeFeed}
      <ActionLink href="/search">Open search</ActionLink>
    {/if}
  {/snippet}
</InfiniteMemeFeed>
