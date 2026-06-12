<script lang="ts">
  import CollectionChip from '$lib/features/collections/CollectionChip.svelte';
  import { bulkGuestGuidance, collectionListBulkOptions } from '$lib/features/memes/bulk-view-model';
  import MemeGrid from '$lib/features/memes/MemeGrid.svelte';
  import { ActionLink, Badge, Button, Card, EmptyState, Input, Notice, PageHeader, Select } from '$lib/ui';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

  const resultStart = $derived(data.page.total === 0 ? 0 : data.offset + 1);
  const resultEnd = $derived(Math.min(data.offset + data.page.items.length, data.page.total));
  const previousOffset = $derived(Math.max(data.offset - data.page.limit, 0));
  const nextOffset = $derived(data.offset + data.page.limit);
  const memes = $derived(data.page.items.map((item) => item.meme));
  const bulkOptions = $derived(collectionListBulkOptions(data.collections));
  const accountType = $derived(data.session?.user.account_type ?? null);
  const bulkGuidance = $derived(bulkGuestGuidance(accountType, bulkOptions.some((collection) => collection.kind === 'custom')));

  function pageHref(offset: number): string {
    const params = new URLSearchParams();

    if (data.query) {
      params.set('q', data.query);
    }

    if (offset > 0) {
      params.set('offset', String(offset));
    }

    const query = params.toString();
    return query ? `/?${query}` : '/';
  }
</script>

<PageHeader title="Find the right meme fast." description="Search the public MemeXpert catalog with plain text, or browse what is already popular." badge="Guest access enabled" />

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

{#if data.errorMessage}
  <Notice>{data.errorMessage}</Notice>
{/if}

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

<div class="my-7 flex flex-wrap justify-between gap-3">
  <p class="m-0 text-muted">
    {#if data.query}
      Results for “{data.query}”
    {:else}
      Browsing public memes
    {/if}
  </p>
  <p class="m-0 text-muted">Showing {resultStart}-{resultEnd} of {data.page.total}</p>
</div>

{#if data.page.items.length > 0}
  <MemeGrid
    {memes}
    bulk={{ enabled: true, accountType, saveEnabled: true, collectionOptions: bulkOptions, guidance: bulkGuidance }}
  />
{:else if !data.errorMessage}
  <EmptyState title="No memes found" message="Try a shorter phrase, a different synonym, or clear the search box to browse." />
{/if}

<nav class="mt-6 flex flex-wrap gap-2" aria-label="Pagination">
  {#if data.offset > 0}
    <ActionLink variant="secondary" href={pageHref(previousOffset)}>Previous</ActionLink>
  {/if}
  {#if data.page.has_more}
    <ActionLink href={pageHref(nextOffset)}>Next page</ActionLink>
  {/if}
</nav>
