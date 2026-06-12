<script lang="ts">
  import { invalidateAll } from '$app/navigation';
  import { connectedProviderLabels } from '$lib/account/view-model';
  import MemeCard from '$lib/components/MemeCard.svelte';
  import {
    activeCollectionId,
    libraryEmptyText,
    profileCapabilities,
    writableCollectionOptions
  } from '$lib/profile/view-model';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  let selectedCollectionId = $state('');
  let selectorPending = $state(false);
  let selectorMessage = $state<string | null>(null);

  const capabilities = $derived(profileCapabilities(data.session ?? null));
  const providerLabels = $derived(connectedProviderLabels(data.session?.linked_providers ?? null));
  const collectionOptions = $derived(writableCollectionOptions(data.library));
  const hasMultipleCollections = $derived(collectionOptions.length > 1);

  $effect(() => {
    selectedCollectionId = activeCollectionId(data.library);
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

      selectorMessage = 'Active save collection updated.';
      await invalidateAll();
    } catch (error) {
      selectedCollectionId = activeCollectionId(data.library);
      selectorMessage = error instanceof Error ? error.message : 'Could not update active collection.';
    } finally {
      selectorPending = false;
    }
  }
</script>

<section class="profile-hero" aria-labelledby="profile-title">
  <div>
    <span class="pill">Profile library</span>
    <h1 id="profile-title">Your meme shelf.</h1>
    <p class="muted">Favorites, pins, collections, and save routing from this account session.</p>
  </div>
  <aside class="profile-status-card" aria-label="Account and provider status">
    <p class="account-title">{capabilities.accountLabel}</p>
    <p class="account-copy">{capabilities.persistenceText}</p>
    <p class="account-copy">{capabilities.pinText}</p>
    <p class="account-copy">{capabilities.collectionText}</p>
    {#if providerLabels.length > 0}
      <p class="account-copy">Connected: {providerLabels.join(', ')}</p>
    {:else if data.session}
      <p class="account-copy">Connected: none yet</p>
    {/if}
    {#if capabilities.showConnectTelegram}
      <a class="button-link compact" href="/account/telegram?returnTo=/profile">Connect Telegram</a>
    {:else if data.session?.linked_providers.telegram_linked}
      <span class="pill success">Telegram connected</span>
    {/if}
  </aside>
</section>

{#if data.libraryError}
  <p class="notice" role="status">{data.libraryError}</p>
{:else if data.library}
  <section class="library-panel" aria-labelledby="active-save-title">
    <div>
      <h2 id="active-save-title">Active save collection</h2>
      <p class="muted">Card save actions go to this destination.</p>
    </div>
    <label class="selector-label">
      <span>Save into</span>
      <select bind:value={selectedCollectionId} onchange={changeActiveCollection} disabled={selectorPending || !hasMultipleCollections}>
        {#each collectionOptions as collection (collection.id)}
          <option value={collection.id}>{collection.title} ({collection.saved_meme_count})</option>
        {/each}
      </select>
    </label>
    {#if !hasMultipleCollections}
      <p class="muted">{data.session?.user.account_type === 'full' ? 'Create more collections later to switch destinations.' : 'Guests save into Favorites.'}</p>
    {/if}
    {#if selectorMessage}
      <p class="action-status" role="status">{selectorMessage}</p>
    {/if}
  </section>

  <section class="library-panel" aria-labelledby="collections-title">
    <div class="section-heading">
      <h2 id="collections-title">Collections</h2>
      <span class="pill">{data.library.collections.length} total</span>
    </div>
    {#if data.library.collections.length > 0}
      <div class="collection-list">
        {#each data.library.collections as collection (collection.id)}
          <article class="collection-row" class:active={collection.id === data.library.active_save_collection?.id}>
            <div>
              <h3>{collection.title}</h3>
              <p class="muted">{collection.kind === 'favorites' ? 'Default favorites' : collection.visibility} · {collection.saved_meme_count} memes · {collection.role}</p>
            </div>
            {#if collection.id === data.library.active_save_collection?.id}
              <span class="pill success">Active save</span>
            {:else if collection.can_write}
              <span class="pill">Writable</span>
            {:else}
              <span class="pill">View only</span>
            {/if}
          </article>
        {/each}
      </div>
    {:else}
      <p class="muted">Collections will appear after your account session is ready.</p>
    {/if}
  </section>

  <section class="library-section" aria-labelledby="favorites-title">
    <div class="section-heading">
      <h2 id="favorites-title">Favorites</h2>
      <span class="pill">{data.library.favorites.length} memes</span>
    </div>
    {#if data.library.favorites.length > 0}
      <div class="grid" aria-label="Favorite memes">
        {#each data.library.favorites as meme (meme.id)}
          <MemeCard {meme} />
        {/each}
      </div>
    {:else}
      <section class="empty-state">
        <h3>No favorites yet</h3>
        <p class="muted">{libraryEmptyText('favorites', data.session ?? null)}</p>
        <a class="button-link compact secondary" href="/">Browse memes</a>
      </section>
    {/if}
  </section>

  <section class="library-section" aria-labelledby="pins-title">
    <div class="section-heading">
      <h2 id="pins-title">Pinned memes</h2>
      <span class="pill">{data.library.pinned_memes.length} pinned</span>
    </div>
    {#if data.library.pinned_memes.length > 0}
      <div class="grid" aria-label="Pinned memes">
        {#each data.library.pinned_memes as meme (meme.id)}
          <MemeCard {meme} />
        {/each}
      </div>
    {:else}
      <section class="empty-state">
        <h3>No pinned memes yet</h3>
        <p class="muted">{libraryEmptyText('pins', data.session ?? null)}</p>
        {#if capabilities.showConnectTelegram}
          <a class="button-link compact" href="/account/telegram?returnTo=/profile">Connect Telegram</a>
        {/if}
      </section>
    {/if}
  </section>
{/if}
