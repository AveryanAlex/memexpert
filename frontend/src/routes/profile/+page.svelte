<script lang="ts">
  import { invalidateAll } from '$app/navigation';
  import { connectedProviderLabels } from '$lib/account/view-model';
  import { bulkCollectionOptions, bulkGuidanceFromSessionAndCollections } from '$lib/features/memes/bulk-view-model';
  import MemeGrid from '$lib/features/memes/MemeGrid.svelte';
  import LibrarySection from '$lib/features/profile/LibrarySection.svelte';
  import {
    activeCollectionId,
    libraryEmptyText,
    profileCapabilities,
    profilePreferences,
    profileStats,
    writableCollectionOptions
  } from '$lib/profile/view-model';
  import { ActionLink, Badge, Card, EmptyState, Notice, Select } from '$lib/ui';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  let selectedCollectionId = $state('');
  let selectorPending = $state(false);
  let selectorMessage = $state<string | null>(null);

  const capabilities = $derived(profileCapabilities(data.session ?? null));
  const providerLabels = $derived(connectedProviderLabels(data.session?.linked_providers ?? null));
  const collectionOptions = $derived(writableCollectionOptions(data.library));
  const bulkOptions = $derived(bulkCollectionOptions(data.library?.collections));
  const hasMultipleCollections = $derived(collectionOptions.length > 1);
  const stats = $derived(profileStats(data.library));
  const preferences = $derived(profilePreferences(data.session?.user ?? null));
  const bulkGuidance = $derived(bulkGuidanceFromSessionAndCollections(data.session ?? null, bulkOptions));

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

<section class="mb-5 grid items-stretch gap-6 md:grid-cols-[minmax(0,1fr)_minmax(280px,0.55fr)]" aria-labelledby="profile-title">
  <div>
    <Badge>Profile library</Badge>
    <h1 id="profile-title" class="mb-3 mt-4 text-[clamp(2.4rem,8vw,5.4rem)] font-black leading-[0.9] tracking-[-0.075em]">Your meme shelf.</h1>
    <p class="m-0 text-muted">Favorites, pins, collections, and save routing from this account session.</p>
  </div>
  <aside class="grid content-start gap-2 rounded-[28px] border border-success-line bg-success-surface p-5" aria-label="Account and provider status">
    <p class="m-0 font-black">{capabilities.accountLabel}</p>
    <p class="m-0 text-sm text-muted">{capabilities.persistenceText}</p>
    <p class="m-0 text-sm text-muted">{capabilities.pinText}</p>
    <p class="m-0 text-sm text-muted">{capabilities.collectionText}</p>
    {#if providerLabels.length > 0}
      <p class="m-0 text-sm text-muted">Connected: {providerLabels.join(', ')}</p>
    {:else if data.session}
      <p class="m-0 text-sm text-muted">Connected: none yet</p>
    {/if}
    {#if capabilities.showConnectTelegram}
      <ActionLink size="compact" href="/account/telegram?returnTo=/profile">Connect Telegram</ActionLink>
    {:else if data.session?.linked_providers.telegram_linked}
      <Badge tone="success">Telegram connected</Badge>
    {/if}
  </aside>
</section>

<section class="my-4 grid gap-4 md:grid-cols-2" aria-label="Profile settings and stats">
  <Card class="grid gap-3 shadow-none" aria-labelledby="profile-stats-title">
    <div>
      <h2 id="profile-stats-title" class="m-0 text-2xl font-black tracking-[-0.04em]">Library stats</h2>
      <p class="m-0 text-muted">Counts come from the loaded library payload when available.</p>
    </div>
    <div class="grid gap-2 sm:grid-cols-2">
      {#each stats as stat}
        <article class="rounded-[20px] border border-line p-4">
          <p class="m-0 text-sm font-extrabold text-muted">{stat.label}</p>
          <p class="m-0 text-3xl font-black tracking-[-0.04em]">{stat.value}</p>
          <p class="m-0 text-sm text-muted">{stat.detail}</p>
        </article>
      {/each}
    </div>
  </Card>

  <Card class="grid gap-3 shadow-none" aria-labelledby="profile-settings-title">
    <div>
      <h2 id="profile-settings-title" class="m-0 text-2xl font-black tracking-[-0.04em]">Account settings</h2>
      <p class="m-0 text-muted">Current backend account state. Unsupported web mutations are shown honestly.</p>
    </div>
    <div class="grid gap-2">
      {#each preferences as preference}
        <article class="rounded-[20px] border border-line p-4">
          <p class="m-0 text-sm font-extrabold text-muted">{preference.label}</p>
          <p class="m-0 font-black">{preference.value}</p>
          <p class="m-0 text-sm text-muted">{preference.detail}</p>
        </article>
      {/each}
    </div>
  </Card>
</section>

{#if data.libraryError}
  <Notice>{data.libraryError}</Notice>
{:else if data.library}
  <Card class="my-4 grid gap-3 shadow-none" aria-labelledby="active-save-title">
    <div>
      <h2 id="active-save-title" class="m-0 text-2xl font-black tracking-[-0.04em]">Active save collection</h2>
      <p class="m-0 text-muted">Card save actions go to this destination.</p>
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
      <p class="m-0 text-muted">{data.session?.user.account_type === 'full' ? 'Create more collections later to switch destinations.' : 'Guests save into Favorites.'}</p>
    {/if}
    {#if selectorMessage}
      <p class="m-0 text-sm text-muted" role="status">{selectorMessage}</p>
    {/if}
  </Card>

  <Card class="my-4 grid gap-3 shadow-none" aria-labelledby="collections-title">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <h2 id="collections-title" class="m-0 text-2xl font-black tracking-[-0.04em]">Collections</h2>
      <Badge>{data.library.collections.length} total</Badge>
    </div>
    {#if data.library.collections.length > 0}
      <div class="grid gap-2">
        {#each data.library.collections as collection (collection.id)}
          <article class={collection.id === data.library.active_save_collection?.id ? 'grid items-center gap-3 rounded-[20px] border border-success-line bg-success-surface p-4 md:grid-cols-[minmax(0,1fr)_auto]' : 'grid items-center gap-3 rounded-[20px] border border-line p-4 md:grid-cols-[minmax(0,1fr)_auto]'}>
            <div>
              <h3 class="m-0 text-lg font-black"><a class="text-inherit underline decoration-2 underline-offset-4" href={`/collection/${collection.id}`}>{collection.title}</a></h3>
              <p class="m-0 text-muted">{collection.kind === 'favorites' ? 'Default favorites' : collection.visibility} · {collection.saved_meme_count} memes · {collection.role}</p>
            </div>
            {#if collection.id === data.library.active_save_collection?.id}
              <Badge tone="success">Active save</Badge>
            {:else if collection.can_write}
              <Badge>Writable</Badge>
            {:else}
              <Badge>View only</Badge>
            {/if}
          </article>
        {/each}
      </div>
    {:else}
      <p class="m-0 text-muted">Collections will appear after your account session is ready.</p>
    {/if}
  </Card>

  <LibrarySection title="Favorites" count={`${data.library.favorites.length} memes`}>
    {#if data.library.favorites.length > 0}
      <MemeGrid
        memes={data.library.favorites}
        label="Favorite memes"
        bulk={{ enabled: true, saveEnabled: true, collectionOptions: bulkOptions, guidance: bulkGuidance }}
      />
    {:else}
      <EmptyState title="No favorites yet" message={libraryEmptyText('favorites', data.session ?? null)}>
        <ActionLink size="compact" variant="secondary" href="/">Browse memes</ActionLink>
      </EmptyState>
    {/if}
  </LibrarySection>

  <LibrarySection title="Pinned memes" count={`${data.library.pinned_memes.length} pinned`}>
    {#if data.library.pinned_memes.length > 0}
      <MemeGrid
        memes={data.library.pinned_memes}
        label="Pinned memes"
        bulk={{ enabled: true, saveEnabled: true, collectionOptions: bulkOptions, guidance: bulkGuidance }}
      />
    {:else}
      <EmptyState title="No pinned memes yet" message={libraryEmptyText('pins', data.session ?? null)}>
        {#if capabilities.showConnectTelegram}
          <ActionLink size="compact" href="/account/telegram?returnTo=/profile">Connect Telegram</ActionLink>
        {/if}
      </EmptyState>
    {/if}
  </LibrarySection>
{/if}
