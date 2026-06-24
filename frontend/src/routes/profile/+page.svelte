<script lang="ts">
  import { invalidateAll } from '$app/navigation';
  import { ApiError, updateUserPreferences } from '$lib/api/client';
  import type { UserLanguage } from '$lib/api/types';
  import { bulkCollectionOptions, bulkGuidanceFromSessionAndCollections } from '$lib/features/memes/bulk-view-model';
  import MemeGrid from '$lib/features/memes/MemeGrid.svelte';
  import LibrarySection from '$lib/features/profile/LibrarySection.svelte';
  import {
    activeCollectionId,
    libraryEmptyText,
    movePinnedMemeId,
    orderPinnedMemesByIds,
    profileCapabilities,
    profileProviderStatuses,
    profilePreferences,
    profileStats,
    writableCollectionOptions
  } from '$lib/profile/view-model';
  import { ActionLink, Badge, Button, Card, EmptyState, Notice, Select, SortableList } from '$lib/ui';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  let selectedCollectionId = $state('');
  let selectorPending = $state(false);
  let selectorMessage = $state<string | null>(null);
  let nsfwPending = $state(false);
  let nsfwMessage = $state<string | null>(null);
  let selectedLanguage = $state<UserLanguage>('any');
  let languagePending = $state(false);
  let languageMessage = $state<string | null>(null);
  let pinOrderIds = $state<string[]>([]);
  let pinOrderPending = $state(false);
  let pinOrderMessage = $state<string | null>(null);

  const LANGUAGE_OPTIONS: Array<{ value: UserLanguage; label: string }> = [
    { value: 'any', label: 'Any language' },
    { value: 'en', label: 'English' },
    { value: 'ru', label: 'Russian' }
  ];

  const capabilities = $derived(profileCapabilities(data.session ?? null));
  const providerStatuses = $derived(profileProviderStatuses(data.session ?? null));
  const collectionOptions = $derived(writableCollectionOptions(data.library));
  const bulkOptions = $derived(bulkCollectionOptions(data.library?.collections));
  const hasMultipleCollections = $derived(collectionOptions.length > 1);
  const stats = $derived(profileStats(data.profileStats));
  const profileNotes = $derived(data.profileStats?.metadata.notes.filter((note) => note.trim()) ?? []);
  const topTags = $derived(data.profileStats?.top_tags ?? []);
  const topTemplates = $derived(data.profileStats?.top_templates ?? []);
  const preferences = $derived(profilePreferences(data.session?.user ?? null));
  const bulkGuidance = $derived(bulkGuidanceFromSessionAndCollections(data.session ?? null, bulkOptions));
  const libraryPinIds = $derived(data.library?.pinned_memes.map((meme) => meme.id) ?? []);
  const orderedPinnedMemes = $derived(orderPinnedMemesByIds(data.library?.pinned_memes ?? [], pinOrderIds));

  $effect(() => {
    selectedCollectionId = activeCollectionId(data.library);
  });

  $effect(() => {
    pinOrderIds = libraryPinIds;
  });

  $effect(() => {
    selectedLanguage = data.session?.user.language ?? 'any';
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

  async function disableNsfw() {
    nsfwPending = true;
    nsfwMessage = null;

    try {
      await updateUserPreferences({ fetch, body: { nsfw_enabled: false } });
      nsfwMessage = 'NSFW is hidden again.';
      await invalidateAll();
    } catch (error) {
      nsfwMessage = error instanceof ApiError || error instanceof Error ? error.message : 'Could not update NSFW preference.';
    } finally {
      nsfwPending = false;
    }
  }

  async function changeLanguage(event: Event) {
    const nextLanguage = (event.currentTarget as HTMLSelectElement).value;
    if (!isUserLanguage(nextLanguage)) {
      selectedLanguage = data.session?.user.language ?? 'any';
      return;
    }

    if (!data.session || nextLanguage === data.session.user.language) {
      selectedLanguage = nextLanguage;
      return;
    }

    languagePending = true;
    languageMessage = null;
    selectedLanguage = nextLanguage;

    try {
      await updateUserPreferences({ fetch, body: { language: nextLanguage } });
      languageMessage = `Language preference updated to ${languageOptionLabel(nextLanguage)}.`;
      await invalidateAll();
    } catch (error) {
      selectedLanguage = data.session.user.language;
      languageMessage = error instanceof ApiError || error instanceof Error ? error.message : 'Could not update language preference.';
    } finally {
      languagePending = false;
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

  function isUserLanguage(value: string): value is UserLanguage {
    return LANGUAGE_OPTIONS.some((option) => option.value === value);
  }

  function languageOptionLabel(value: UserLanguage): string {
    return LANGUAGE_OPTIONS.find((option) => option.value === value)?.label ?? value;
  }
</script>

<section class="mb-5 grid items-stretch gap-6 md:grid-cols-[minmax(0,1fr)_minmax(280px,0.55fr)]" aria-labelledby="profile-title">
  <div class="rounded-[36px] border border-white/10 bg-white/90 p-6 shadow-[0_24px_70px_rgb(15_23_42_/_14%)]">
    <Badge>Profile library</Badge>
    <h1 id="profile-title" class="mb-3 mt-4 text-[clamp(2.4rem,8vw,5.4rem)] font-black leading-[0.9] tracking-[-0.075em] text-slate-950">Your meme command center.</h1>
    <p class="m-0 text-slate-600">Favorites, pins, collections, preferences, and account connections from this session.</p>
  </div>
  <aside class="grid content-start gap-2 rounded-[28px] border border-white/10 bg-slate-950 p-5 text-white" aria-label="Account and provider status">
    <p class="m-0 font-black">{capabilities.accountLabel}</p>
    <p class="m-0 text-sm text-muted">{capabilities.persistenceText}</p>
    <p class="m-0 text-sm text-muted">{capabilities.pinText}</p>
    <p class="m-0 text-sm text-muted">{capabilities.collectionText}</p>
    <div class="mt-2 grid gap-2">
      {#each providerStatuses as provider}
        <article class="rounded-[18px] border border-success-line/70 bg-paper/70 p-3">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <p class="m-0 text-sm font-extrabold text-muted">{provider.label}</p>
            <Badge tone={provider.value === 'Connected' || provider.value === 'Verified' || provider.value === 'Password set' ? 'success' : 'neutral'}>{provider.value}</Badge>
          </div>
          <p class="m-0 mt-1 text-sm text-muted">{provider.detail}</p>
        </article>
      {/each}
    </div>
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
      <h2 id="profile-stats-title" class="m-0 text-2xl font-black tracking-[-0.04em]">Interaction stats</h2>
      <p class="m-0 text-muted">Counts come from your recorded meme interaction history.</p>
    </div>
    {#if data.profileStatsError}
      <Notice>{data.profileStatsError}</Notice>
    {/if}
    <div class="grid gap-2 sm:grid-cols-2">
      {#each stats as stat}
        <article class="rounded-[20px] border border-line p-4">
          <p class="m-0 text-sm font-extrabold text-muted">{stat.label}</p>
          <p class="m-0 text-3xl font-black tracking-[-0.04em]">{stat.value}</p>
          <p class="m-0 text-sm text-muted">{stat.detail}</p>
        </article>
      {/each}
    </div>
    {#if profileNotes.length > 0}
      <div class="rounded-[20px] border border-line bg-soft/50 p-4" aria-label="Profile stats notes">
        <p class="m-0 font-black">Stats notes</p>
        <ul class="m-0 mt-2 grid gap-1 pl-5 text-sm text-muted">
          {#each profileNotes as note}
            <li>{note}</li>
          {/each}
        </ul>
      </div>
    {/if}
    {#if topTags.length > 0 || topTemplates.length > 0}
      <div class="grid gap-3 lg:grid-cols-2">
        {#if topTags.length > 0}
          <section class="rounded-[20px] border border-line p-4" aria-labelledby="profile-top-tags-title">
            <h3 id="profile-top-tags-title" class="m-0 text-lg font-black tracking-[-0.03em]">Top tags from your history</h3>
            <div class="mt-3 grid gap-2">
              {#each topTags as tag}
                <a class="flex items-center justify-between gap-3 rounded-[16px] border border-line bg-paper px-3 py-2 font-extrabold text-inherit no-underline hover:bg-soft" href={`/tags/${encodeURIComponent(tag.tag)}`}>
                  <span>#{tag.tag}</span>
                  <span class="text-muted">{tag.count}</span>
                </a>
              {/each}
            </div>
          </section>
        {/if}
        {#if topTemplates.length > 0}
          <section class="rounded-[20px] border border-line p-4" aria-labelledby="profile-top-templates-title">
            <h3 id="profile-top-templates-title" class="m-0 text-lg font-black tracking-[-0.03em]">Top templates from your history</h3>
            <div class="mt-3 grid gap-2">
              {#each topTemplates as template}
                <a class="flex items-center justify-between gap-3 rounded-[16px] border border-line bg-paper px-3 py-2 font-extrabold text-inherit no-underline hover:bg-soft" href={`/templates/${encodeURIComponent(template.slug)}`}>
                  <span>{template.name}</span>
                  <span class="text-muted">{template.count}</span>
                </a>
              {/each}
            </div>
          </section>
        {/if}
      </div>
    {/if}
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
    <div class="rounded-[20px] border border-line bg-soft/50 p-4">
      <p class="m-0 font-black">Language preference</p>
      <p class="m-0 mb-3 text-sm text-muted">Choose the account language used by account-aware surfaces when supported.</p>
      <label class="grid max-w-[420px] gap-2 font-extrabold text-chiptext">
        <span>Profile language</span>
        <Select bind:value={selectedLanguage} onchange={changeLanguage} disabled={!data.session || languagePending}>
          {#each LANGUAGE_OPTIONS as option}
            <option value={option.value}>{option.label}</option>
          {/each}
        </Select>
      </label>
      {#if languageMessage}
        <p class="m-0 mt-3 text-sm text-muted" role="status">{languageMessage}</p>
      {/if}
    </div>
    <div class="rounded-[20px] border border-line bg-soft/50 p-4">
      {#if data.session?.user.nsfw_enabled}
        <p class="m-0 font-black">NSFW search is enabled.</p>
        <p class="m-0 mb-3 text-sm text-muted">Turn it off to keep NSFW memes filtered from public discovery again.</p>
        <Button type="button" variant="secondary" size="compact" onclick={disableNsfw} disabled={nsfwPending}>{nsfwPending ? 'Saving...' : 'Turn off NSFW'}</Button>
      {:else}
        <p class="m-0 font-black">NSFW stays hidden.</p>
        <p class="m-0 mb-3 text-sm text-muted">To include NSFW results, use the NSFW filter on Search and confirm the opt-in there.</p>
        <ActionLink size="compact" variant="secondary" href="/search">Open Search filters</ActionLink>
      {/if}
      {#if nsfwMessage}
        <p class="m-0 mt-3 text-sm text-muted" role="status">{nsfwMessage}</p>
      {/if}
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
        showAccessMarkers={Boolean(data.session)}
      />
    {:else}
      <EmptyState title="No favorites yet" message={libraryEmptyText('favorites', data.session ?? null)}>
        <ActionLink size="compact" variant="secondary" href="/">Browse memes</ActionLink>
      </EmptyState>
    {/if}
  </LibrarySection>

  <LibrarySection title="Pinned memes" count={`${data.library.pinned_memes.length} pinned`}>
    {#if data.library.pinned_memes.length > 0}
      <Card class="mb-4 grid gap-3 shadow-none" aria-labelledby="pin-order-title">
        <div>
          <h3 id="pin-order-title" class="m-0 text-xl font-black tracking-[-0.03em]">Pin order</h3>
          <p class="m-0 text-muted">Use Up/Down controls for keyboard-safe ordering, or drag rows onto a new position.</p>
        </div>
        <SortableList
          items={orderedPinnedMemes}
          onReorder={savePinOrder}
          disabled={pinOrderPending}
          class="grid gap-2"
          itemElement="article"
          itemClass="grid gap-2 rounded-[18px] border border-line bg-paper p-3 sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:items-center"
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
        showAccessMarkers={Boolean(data.session)}
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
