<script lang="ts">
  import CollectionChip from '$lib/features/collections/CollectionChip.svelte';
  import { bulkGuidanceFromSessionAndCollections, collectionListBulkOptions } from '$lib/features/memes/bulk-view-model';
  import InfiniteMemeFeed from '$lib/features/memes/InfiniteMemeFeed.svelte';
  import type { MemeFeedSource } from '$lib/features/memes/infinite-feed';
  import MemeOfTheDayPanel from '$lib/features/memes/MemeOfTheDayPanel.svelte';
  import { ActionLink, Button, Card, Input, Notice, Select } from '$lib/ui';
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

<section class="mb-6 grid gap-4 rounded-[36px] border border-white/10 bg-white/90 p-6 shadow-[0_30px_80px_rgb(15_23_42_/_18%)] md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
  <div>
    <p class="m-0 text-sm font-black uppercase tracking-[0.18em] text-blue-700">For You</p>
    <h1 class="mb-3 mt-2 text-[clamp(2.4rem,7vw,5.8rem)] font-black leading-[0.9] tracking-[-0.08em] text-slate-950">Your meme feed, tuned by every save.</h1>
    <p class="m-0 max-w-2xl text-lg text-slate-600">Search globally from the top bar, browse recommendations here, or jump into trend analytics when you want the internet’s current pulse.</p>
  </div>
  <div class="flex flex-wrap gap-2 md:justify-end">
    <ActionLink href="/trends">Open Trends</ActionLink>
    <ActionLink variant="secondary" href="/search">Advanced search</ActionLink>
  </div>
</section>

<MemeOfTheDayPanel memeOfTheDay={data.memeOfTheDay} initialError={data.memeOfTheDayErrorMessage} showAccessMarkers={Boolean(data.session)} />

<Card class="my-6 grid gap-4 border-white/70 bg-white/90 shadow-[0_20px_60px_rgb(15_23_42_/_12%)]" aria-labelledby="collections-title">
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
  showAccessMarkers={Boolean(data.session)}
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
