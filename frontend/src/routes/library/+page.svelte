<script lang="ts">
  import { invalidateAll } from '$app/navigation';
  import { readAuthState } from '$lib/auth-state';
  import type { UserRead } from '$lib/api/types';
  import { bulkCollectionOptions, bulkGuidanceFromSessionAndCollections } from '$lib/features/memes/bulk-view-model';
  import MemeGrid from '$lib/features/memes/MemeGrid.svelte';
  import LibrarySection from '$lib/features/profile/LibrarySection.svelte';
  import {
    activeCollectionId,
    libraryEmptyText,
    movePinnedMemeId,
    orderPinnedMemesByIds,
    profileCapabilities,
    writableCollectionOptions
  } from '$lib/profile/view-model';
  import { ActionLink, Badge, Button, Card, EmptyState, Input, Notice, Select, SortableList } from '$lib/ui';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form?: ActionData } = $props();

  const authState = readAuthState(() => ({ session: data.session ?? null, sessionError: data.sessionError }));
  const session = $derived($authState.session);

  let selectedCollectionId = $state('');
  let selectorPending = $state(false);
  let selectorMessage = $state<string | null>(null);
  let pinOrderIds = $state<string[]>([]);
  let pinOrderPending = $state(false);
  let pinOrderMessage = $state<string | null>(null);

  const capabilities = $derived(profileCapabilities(session));
  const collectionOptions = $derived(writableCollectionOptions(data.library));
  const bulkOptions = $derived(bulkCollectionOptions(data.library?.collections));
  const hasMultipleCollections = $derived(collectionOptions.length > 1);
  const bulkGuidance = $derived(bulkGuidanceFromSessionAndCollections(session, bulkOptions));
  const libraryPinIds = $derived(data.library?.pinned_memes.map((meme) => meme.id) ?? []);
  const orderedPinnedMemes = $derived(orderPinnedMemesByIds(data.library?.pinned_memes ?? [], pinOrderIds));

  $effect(() => {
    selectedCollectionId = activeCollectionId(data.library);
  });

  $effect(() => {
    pinOrderIds = libraryPinIds;
  });

  async function changeActiveCollection(event: Event) {
    const nextCollectionId = (event.currentTarget as HTMLSelectElement).value;
    if (!nextCollectionId || nextCollectionId === activeCollectionId(data.library)) {
      selectedCollectionId = nextCollectionId;
      return;
    }

    selectorPending = true;
    selectorMessage = null;
    selectedCollectionId = nextCollectionId;

    try {
      const response = await fetch('/api/v1/memes/active-save-collection', {
        method: 'PUT',
        credentials: 'include',
        headers: { accept: 'application/json', 'content-type': 'application/json' },
        body: JSON.stringify({ collection_id: nextCollectionId })
      });

      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(typeof payload?.detail === 'string' ? payload.detail : 'Could not update active collection.');
      }

      authState.updateUser((await response.json()) as UserRead);
      selectorMessage = 'Active save collection updated.';
      await invalidateAll();
    } catch (error) {
      selectedCollectionId = activeCollectionId(data.library);
      selectorMessage = error instanceof Error ? error.message : 'Could not update active collection.';
    } finally {
      selectorPending = false;
    }
  }

  async function movePin(memeId: string, direction: -1 | 1) {
    await savePinOrder(movePinnedMemeId(pinOrderIds, memeId, direction));
  }

  async function savePinOrder(nextIds: string[]) {
    if (pinOrderPending || nextIds.join('|') === pinOrderIds.join('|')) return;

    const previousIds = pinOrderIds;
    pinOrderIds = nextIds;
    pinOrderPending = true;
    pinOrderMessage = 'Saving pin order...';

    try {
      const response = await fetch('/api/v1/memes/pins/reorder', {
        method: 'PUT',
        credentials: 'include',
        headers: { accept: 'application/json', 'content-type': 'application/json', 'x-requested-with': 'XMLHttpRequest' },
        body: JSON.stringify({ meme_ids: nextIds })
      });

      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(typeof payload?.detail === 'string' ? payload.detail : 'Could not reorder pinned memes.');
      }

      pinOrderMessage = 'Pin order saved.';
      await invalidateAll();
    } catch (error) {
      pinOrderIds = previousIds;
      pinOrderMessage = error instanceof Error ? error.message : 'Could not reorder pinned memes.';
    } finally {
      pinOrderPending = false;
    }
  }
</script>

<section class="mb-5 flex flex-wrap items-end justify-between gap-4" aria-labelledby="library-title">
  <div>
    <Badge>Saved</Badge>
    <h1 id="library-title" class="m-0 mt-3 text-[clamp(2rem,5vw,4rem)] font-black leading-[0.95] tracking-[-0.055em]">Your saved memes</h1>
    <p class="m-0 mt-2 max-w-2xl text-muted">Favorites, collections, and pins in one place.</p>
  </div>
  <nav class="flex flex-wrap gap-2" aria-label="Saved library sections">
    <a class="rounded-full border border-line bg-paper px-3 py-2 text-sm font-extrabold no-underline hover:bg-soft" href="#favorites">Favorites</a>
    <a class="rounded-full border border-line bg-paper px-3 py-2 text-sm font-extrabold no-underline hover:bg-soft" href="#collections">Collections</a>
    <a class="rounded-full border border-line bg-paper px-3 py-2 text-sm font-extrabold no-underline hover:bg-soft" href="#pins">Pins</a>
  </nav>
</section>

{#if session?.user.account_type === 'full'}
  <details class="my-4 rounded-xl border border-line bg-paper" open={Boolean(form?.collectionError || form?.collectionCreatedId)}>
    <summary class="cursor-pointer px-5 py-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent">
      <span class="font-black">New collection</span>
      <span class="ml-2 text-sm text-muted">Keep a set of saved memes together</span>
    </summary>
    <div class="border-t border-line p-4 sm:p-5">
      {#if form?.collectionError}
        <Notice role="alert" tone="danger">{form.collectionError}</Notice>
      {:else if form?.collectionCreatedId}
        <Card class="mb-4 flex flex-wrap items-center justify-between gap-3 border-success-line bg-success-surface" aria-label="Collection creation result">
          <p class="m-0 font-extrabold text-success-text" role="status">{form.successMessage ?? 'Collection created.'}</p>
          <ActionLink size="compact" href={`/collection/${encodeURIComponent(form.collectionCreatedId)}`}>Open collection</ActionLink>
        </Card>
      {/if}

      <form class="grid gap-3 sm:grid-cols-2" method="POST" action="?/createCollection">
        <label class="grid gap-2 font-extrabold text-chiptext">
          <span>Title</span>
          <Input name="title" placeholder="New collection title" maxlength={120} required aria-label="New collection title" />
        </label>
        <label class="grid gap-2 font-extrabold text-chiptext">
          <span>Visibility</span>
          <Select name="visibility" aria-label="Collection visibility">
            <option value="private">Private</option>
            <option value="unlisted">Unlisted</option>
          </Select>
        </label>
        <label class="grid gap-2 font-extrabold text-chiptext sm:col-span-2">
          <span>Description</span>
          <Input name="description" placeholder="Description" aria-label="Collection description" />
        </label>
        <div class="sm:col-span-2">
          <Button type="submit">Create collection</Button>
        </div>
      </form>
    </div>
  </details>
{/if}

{#if data.libraryError}
  <Notice>{data.libraryError}</Notice>
{:else if data.library}
  <Card class="my-4 grid gap-3 shadow-none" aria-labelledby="active-save-title">
    <div>
      <h2 id="active-save-title" class="m-0 text-xl font-black tracking-[-0.03em]">Active save destination</h2>
      <p class="m-0 text-muted">New Save actions go to this collection.</p>
    </div>
    <label class="grid gap-2 font-extrabold text-chiptext">
      <span>Save into</span>
      <Select class="w-full max-w-[420px]" bind:value={selectedCollectionId} onchange={changeActiveCollection} disabled={selectorPending || !hasMultipleCollections}>
        {#each collectionOptions as collection (collection.id)}
          <option value={collection.id}>{collection.title} ({collection.saved_meme_count})</option>
        {/each}
      </Select>
    </label>
    {#if !hasMultipleCollections}
      <p class="m-0 text-sm text-muted">{session?.user.account_type === 'full' ? 'Create more collections later to switch destinations.' : 'Guests save into Favorites.'}</p>
    {/if}
    {#if selectorMessage}
      <p class="m-0 text-sm text-muted" role="status">{selectorMessage}</p>
    {/if}
  </Card>

  <section id="collections" class="my-7" aria-labelledby="collections-title">
    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <h2 id="collections-title" class="m-0 text-2xl font-black tracking-[-0.04em]">Collections</h2>
      <Badge>{data.library.collections.length} total</Badge>
    </div>
    {#if data.library.collections.length > 0}
      <div class="grid gap-2">
        {#each data.library.collections as collection (collection.id)}
          <article class={collection.id === data.library.active_save_collection?.id ? 'grid items-center gap-3 rounded-xl border border-success-line bg-success-surface p-4 md:grid-cols-[minmax(0,1fr)_auto]' : 'grid items-center gap-3 rounded-xl border border-line bg-paper p-4 md:grid-cols-[minmax(0,1fr)_auto]'}>
            <div>
              <h3 class="m-0 text-lg font-black"><a class="text-inherit underline decoration-2 underline-offset-4" href={`/collection/${collection.id}`}>{collection.title}</a></h3>
              <p class="m-0 text-sm text-muted">{collection.saved_meme_count} meme{collection.saved_meme_count === 1 ? '' : 's'} · {collection.kind === 'favorites' ? 'Favorites' : collection.visibility} · {collection.role}</p>
            </div>
            {#if collection.id === data.library.active_save_collection?.id}
              <Badge tone="success">Active save</Badge>
            {:else if collection.can_write}
              <Badge>Can save</Badge>
            {:else}
              <Badge>View only</Badge>
            {/if}
          </article>
        {/each}
      </div>
    {:else}
      <p class="m-0 text-muted">Collections will appear after your account session is ready.</p>
    {/if}
  </section>

  <div id="favorites">
    <LibrarySection title="Favorites" count={`${data.library.favorites.length} memes`}>
      {#if data.library.favorites.length > 0}
        <MemeGrid
          memes={data.library.favorites}
          label="Favorite memes"
          bulk={{ enabled: true, saveEnabled: true, collectionOptions: bulkOptions, guidance: bulkGuidance }}
          showAccessMarkers={Boolean(session)}
        />
      {:else}
        <EmptyState title="No favorites yet" message={libraryEmptyText('favorites', session)}>
          <ActionLink size="compact" variant="secondary" href="/">Browse memes</ActionLink>
        </EmptyState>
      {/if}
    </LibrarySection>
  </div>

  <div id="pins">
    <LibrarySection title="Pins" count={`${data.library.pinned_memes.length} pinned`}>
      {#if data.library.pinned_memes.length > 0}
        <Card class="mb-4 grid gap-3 shadow-none" aria-labelledby="pin-order-title">
          <div>
            <h3 id="pin-order-title" class="m-0 text-xl font-black tracking-[-0.03em]">Pin order</h3>
            <p class="m-0 text-muted">Use Up and Down controls for keyboard-safe ordering, or drag rows to a new position.</p>
          </div>
          <SortableList
            items={orderedPinnedMemes}
            onReorder={savePinOrder}
            disabled={pinOrderPending}
            class="grid gap-2"
            itemElement="article"
            itemClass="grid gap-2 rounded-xl border border-line bg-paper p-3 sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:items-center"
            aria-live="polite"
            aria-busy={pinOrderPending}
          >
            {#snippet children(meme, index, controls)}
              <span {@attach controls.attachHandle} class="cursor-grab rounded-full border border-line bg-soft px-3 py-1 text-sm font-black text-muted active:cursor-grabbing focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink">Drag</span>
              <div>
                <p class="m-0 font-black">{meme.caption || meme.tags[0] || `Pinned meme ${index + 1}`}</p>
                <p class="m-0 text-sm text-muted">Position {index + 1} of {orderedPinnedMemes.length}</p>
              </div>
              <div class="flex flex-wrap gap-2 sm:justify-end">
                <Button size="compact" variant="secondary" type="button" onclick={() => movePin(meme.id, -1)} disabled={pinOrderPending || index === 0}>Up</Button>
                <Button size="compact" variant="secondary" type="button" onclick={() => movePin(meme.id, 1)} disabled={pinOrderPending || index === orderedPinnedMemes.length - 1}>Down</Button>
              </div>
            {/snippet}
          </SortableList>
          {#if pinOrderMessage}
            <p class="m-0 text-sm text-muted" role="status">{pinOrderMessage}</p>
          {/if}
        </Card>
        <MemeGrid
          memes={orderedPinnedMemes}
          label="Pinned memes"
          bulk={{ enabled: true, saveEnabled: true, collectionOptions: bulkOptions, guidance: bulkGuidance }}
          showAccessMarkers={Boolean(session)}
        />
      {:else}
        <EmptyState title="No pins yet" message={libraryEmptyText('pins', session)}>
          {#if capabilities.showConnectTelegram}
            <ActionLink size="compact" href="/account/telegram?returnTo=/library">Connect Telegram</ActionLink>
          {/if}
        </EmptyState>
      {/if}
    </LibrarySection>
  </div>
{/if}
