<script lang="ts">
  import { goto } from '$app/navigation';
  import { SlidersHorizontal, X } from '@lucide/svelte';
  import { untrack } from 'svelte';
  import { ApiError, updateUserPreferences } from '$lib/api/client';
  import type { ContentKind, ContentLanguage, CurrentSessionRead, MemeSearchScope, WebCollectionListRead } from '$lib/api/types';
  import { readAuthState } from '$lib/auth-state';
  import { ActionLink, Button, FormRow, Input, Notice, Select } from '$lib/ui';
  import * as Dialog from '$lib/ui/dialog';
  import {
    buildSearchHref,
    LANGUAGE_OPTIONS,
    MEDIA_TYPE_OPTIONS,
    normalizeCollectionIds,
    normalizeTags,
    SEARCH_SCOPE_OPTIONS,
    type SearchRouteState
  } from '$lib/searchParams';

  interface Props {
    filters: SearchRouteState;
    session: CurrentSessionRead | null;
    collections: WebCollectionListRead | null;
    collectionErrorMessage: string | null;
  }

  type ScopeOption = { value: MemeSearchScope; label: string; description: string };

  let { filters, session, collections, collectionErrorMessage }: Props = $props();

  const authState = readAuthState(() => ({ session, sessionError: null }));
  const currentSession = $derived($authState.session);

  const formId = 'search-results-form';
  const scopeOptions: ScopeOption[] = [
    { value: 'all', label: 'Everywhere', description: 'Search public and saved memes you can open.' },
    { value: 'public', label: 'Public memes', description: 'Search memes anyone can enjoy.' },
    { value: 'private', label: 'My saved memes', description: 'Search memes saved to your account.' },
    { value: 'collections', label: 'Specific collections', description: 'Choose the saved collections to search.' }
  ];

  const initialFilters = untrack(() => filters);
  let query = $state(initialFilters.query);
  let tags = $state(initialFilters.tags.join(', '));
  let includeNsfw = $state(String(initialFilters.includeNsfw));
  let mediaType = $state<ContentKind | ''>(initialFilters.mediaType ?? '');
  let language = $state<ContentLanguage | ''>(initialFilters.language ?? '');
  let selectedScope = $state<MemeSearchScope>(initialFilters.scope);
  let selectedCollectionIds = $state<string[]>([...initialFilters.collectionIds]);
  let filtersOpen = $state(false);
  let nsfwGateOpen = $state(false);
  let pendingSearchHref = $state<string | null>(null);
  let nsfwGatePending = $state(false);
  let nsfwGateMessage = $state<string | null>(null);
  let syncedFilterKey = $state('');
  let wasFiltersOpen = $state(false);
  let hydrated = $state(false);
  let scopeValidationMessage = $state<string | null>(null);

  const readableCollections = $derived(collections?.collections.filter((item) => item.capabilities.can_view) ?? []);
  const selectedCollectionSet = $derived(new Set(selectedCollectionIds));
  const collectionScopeActive = $derived(selectedScope === 'collections');
  const selectedCollectionCount = $derived(selectedCollectionIds.length);
  const collectionSelectionInvalid = $derived(collectionScopeActive && selectedCollectionCount === 0);
  const activeFilterCount = $derived(
    filters.tags.length +
      (filters.includeNsfw ? 1 : 0) +
      (filters.mediaType ? 1 : 0) +
      (filters.language ? 1 : 0) +
      (filters.scope !== 'public' ? 1 : 0) +
      (filters.scope === 'collections' ? filters.collectionIds.length : 0)
  );
  const shouldConfirmNsfw = $derived(currentSession?.user.nsfw_enabled === false);
  const nsfwRequestedButDisabled = $derived(filters.includeNsfw && currentSession?.user.nsfw_enabled !== true);

  $effect(() => {
    const nextFilterKey = JSON.stringify(filters);
    if (nextFilterKey === syncedFilterKey) return;

    syncedFilterKey = nextFilterKey;
    resetDraft();
  });

  $effect(() => {
    if (wasFiltersOpen && !filtersOpen) resetDraft(true);
    wasFiltersOpen = filtersOpen;
  });

  $effect(() => {
    hydrated = true;
  });

  function resetDraft(preserveQuery = false) {
    if (!preserveQuery) query = filters.query;
    tags = filters.tags.join(', ');
    includeNsfw = String(filters.includeNsfw);
    mediaType = filters.mediaType ?? '';
    language = filters.language ?? '';
    selectedScope = filters.scope;
    selectedCollectionIds = [...filters.collectionIds];
  }

  function currentSearchState(): SearchRouteState {
    const scope = SEARCH_SCOPE_OPTIONS.some((option) => option.value === selectedScope) ? selectedScope : 'public';
    const nextMediaType = MEDIA_TYPE_OPTIONS.find((option) => option.value === mediaType)?.value ?? null;
    const nextLanguage = LANGUAGE_OPTIONS.find((option) => option.value === language)?.value ?? null;

    return {
      query: query.trim(),
      tags: normalizeTags([tags]),
      includeNsfw: includeNsfw === 'true',
      mediaType: nextMediaType,
      language: nextLanguage,
      scope,
      collectionIds: scope === 'collections' ? normalizeCollectionIds(selectedCollectionIds) : [],
      offset: 0
    };
  }

  function handleSearchSubmit(event: SubmitEvent) {
    event.preventDefault();
    const nextFilters = currentSearchState();
    if (nextFilters.scope === 'collections' && nextFilters.collectionIds.length === 0) {
      scopeValidationMessage = 'Choose at least one collection before searching.';
      filtersOpen = true;
      return;
    }

    scopeValidationMessage = null;
    const href = buildSearchHref(nextFilters, { offset: 0 });

    filtersOpen = false;
    if (!nextFilters.includeNsfw || !shouldConfirmNsfw) {
      void goto(href);
      return;
    }

    pendingSearchHref = href;
    nsfwGateMessage = null;
    nsfwGateOpen = true;
  }

  function cancelNsfwOptIn() {
    nsfwGateOpen = false;
    pendingSearchHref = null;
    nsfwGateMessage = null;
    includeNsfw = 'false';
  }

  async function confirmNsfwOptIn() {
    if (!pendingSearchHref) return;

    nsfwGatePending = true;
    nsfwGateMessage = null;
    try {
      const user = await updateUserPreferences({ fetch, body: { nsfw_enabled: true } });
      authState.updateUser(user);
      const href = pendingSearchHref;
      nsfwGateOpen = false;
      pendingSearchHref = null;
      await goto(href, { invalidateAll: true });
    } catch (error) {
      nsfwGateMessage = error instanceof ApiError || error instanceof Error ? error.message : 'Could not update the sensitive-content preference.';
    } finally {
      nsfwGatePending = false;
    }
  }
</script>

<form id={formId} class="flex flex-col gap-3 sm:flex-row" method="GET" action="/search" onsubmit={handleSearchSubmit}>
  <label class="sr-only" for="search-results-query">Search memes</label>
  <Input id="search-results-query" class="min-w-0 flex-1" name="q" type="search" placeholder="Search memes" bind:value={query} />
  {#if !filtersOpen}
    {#each filters.tags as tag}
      <input type="hidden" name="tags" value={tag} />
    {/each}
    <input type="hidden" name="include_nsfw" value={String(filters.includeNsfw)} />
    {#if filters.mediaType}<input type="hidden" name="media_type" value={filters.mediaType} />{/if}
    {#if filters.language}<input type="hidden" name="language" value={filters.language} />{/if}
    <input type="hidden" name="scope" value={filters.scope} />
    {#if filters.scope === 'collections'}
      {#each filters.collectionIds as collectionId}
        <input type="hidden" name="collection_ids" value={collectionId} />
      {/each}
    {/if}
  {/if}
  <div class="flex gap-2">
    <Button type="submit" size="compact" class="flex-1 sm:flex-none">Search</Button>
    <Dialog.Root bind:open={filtersOpen}>
      <Dialog.Trigger type="button" disabled={!hydrated} class="!rounded-[14px] !border !border-line !bg-paper !px-3 !py-2 !text-ink hover:!bg-soft disabled:!cursor-wait disabled:!opacity-70">
        <SlidersHorizontal class="size-4" aria-hidden="true" />
        Filters
        {#if activeFilterCount > 0}
          <span class="grid size-5 place-items-center rounded-full bg-soft text-xs" aria-label={`${activeFilterCount} active filters`}>{activeFilterCount}</span>
        {/if}
      </Dialog.Trigger>
      <Dialog.Content
        class="!left-0 !right-0 !top-auto !bottom-0 !w-full !max-w-none !translate-x-0 !translate-y-0 !gap-0 !overflow-hidden !rounded-b-none !rounded-t-[18px] !p-0 md:!top-0 md:!bottom-0 md:!left-auto md:!right-0 md:!w-[380px] md:!max-w-[380px] md:!rounded-none"
        aria-labelledby="search-filter-drawer-title"
        aria-describedby="search-filter-drawer-description"
      >
        <div class="grid max-h-[85dvh] grid-rows-[auto_minmax(0,1fr)_auto] overflow-hidden md:h-dvh md:max-h-none">
          <div class="flex items-start justify-between gap-4 border-b border-line px-4 py-4">
            <div>
              <Dialog.Title id="search-filter-drawer-title" class="m-0 text-xl font-bold tracking-[-0.03em]">Filters</Dialog.Title>
              <Dialog.Description id="search-filter-drawer-description" class="m-0 mt-1 text-sm text-muted">Choose what to include in your search.</Dialog.Description>
            </div>
            <Dialog.Close class="grid size-9 place-items-center rounded-[12px] text-muted hover:bg-soft hover:text-ink" aria-label="Close filters">
              <X class="size-4" aria-hidden="true" />
            </Dialog.Close>
          </div>

          <div class="min-h-0 overflow-y-auto px-4 py-5">
            <div class="grid gap-5">
              <FormRow label="Tags or categories" hint="Separate ideas with commas.">
                <Input form={formId} name="tags" placeholder="reaction, wholesome, work" bind:value={tags} />
              </FormRow>

              <div class="grid gap-3 sm:grid-cols-2 md:grid-cols-1">
                <FormRow label="Media type">
                  <Select form={formId} name="media_type" bind:value={mediaType}>
                    <option value="">Any type</option>
                    {#each MEDIA_TYPE_OPTIONS as option}
                      <option value={option.value}>{option.label}</option>
                    {/each}
                  </Select>
                </FormRow>
                <FormRow label="Language">
                  <Select form={formId} name="language" bind:value={language}>
                    <option value="">Any language</option>
                    {#each LANGUAGE_OPTIONS as option}
                      <option value={option.value}>{option.label}</option>
                    {/each}
                  </Select>
                </FormRow>
              </div>

              <FormRow label="Sensitive content">
                <Select form={formId} name="include_nsfw" bind:value={includeNsfw}>
                  <option value="false">Hide sensitive content</option>
                  <option value="true">Include sensitive content</option>
                </Select>
              </FormRow>

              <fieldset class="grid gap-3" aria-describedby="search-scope-help">
                <legend class="font-semibold text-ink">Where to search</legend>
                <p id="search-scope-help" class="m-0 text-sm text-muted">Choose the memes you want to browse.</p>
                <div class="grid gap-2">
                  {#each scopeOptions as option}
                    <label class={selectedScope === option.value ? 'grid grid-cols-[auto_minmax(0,1fr)] gap-3 rounded-xl border border-ink bg-soft p-3 text-ink' : 'grid grid-cols-[auto_minmax(0,1fr)] gap-3 rounded-xl border border-line bg-paper p-3 text-ink'}>
                      <input form={formId} class="mt-1 accent-accent" type="radio" name="scope" value={option.value} bind:group={selectedScope} />
                      <span class="grid gap-1">
                        <span class="font-semibold leading-tight">{option.label}</span>
                        <span class="text-sm font-normal leading-snug text-muted">{option.description}</span>
                      </span>
                    </label>
                  {/each}
                </div>
              </fieldset>

              <fieldset class={collectionScopeActive ? 'grid gap-3 rounded-xl border border-accent/35 bg-soft/60 p-3' : 'grid gap-3 rounded-xl border border-line bg-paper p-3'} aria-describedby="collection-filter-state">
                <legend class="px-1 font-semibold text-ink">Collections</legend>
                {#if collectionErrorMessage}
                  <Notice tone="danger" role="status" class="my-0">{collectionErrorMessage}</Notice>
                {/if}

                {#if readableCollections.length > 0}
                  <div class="grid gap-2">
                    {#each readableCollections as item (item.collection.id)}
                      <label class={selectedCollectionSet.has(item.collection.id) ? 'grid grid-cols-[auto_minmax(0,1fr)] gap-2 rounded-lg border border-ink bg-soft px-3 py-2 text-sm text-ink' : 'grid grid-cols-[auto_minmax(0,1fr)] gap-2 rounded-lg border border-line bg-paper px-3 py-2 text-sm text-ink'}>
                        <input
                          form={formId}
                          class="mt-1 accent-accent"
                          type="checkbox"
                          name="collection_ids"
                          value={item.collection.id}
                          bind:group={selectedCollectionIds}
                        />
                        <span class="min-w-0 truncate font-semibold">{item.collection.title}</span>
                      </label>
                    {/each}
                  </div>
                  <p id="collection-filter-state" class="m-0 text-sm text-muted">
                    {#if collectionScopeActive}
                      {selectedCollectionCount > 0 ? `${selectedCollectionCount} collection${selectedCollectionCount === 1 ? '' : 's'} selected.` : 'Choose one or more collections.'}
                    {:else}
                      Select Specific collections above to narrow your search.
                    {/if}
                  </p>
                {:else}
                  <p id="collection-filter-state" class="m-0 text-sm text-muted">
                    {#if collectionErrorMessage}
                      Collection choices could not load. You can still search public and saved memes.
                    {:else if session && collections}
                      You do not have saved collections to search yet.
                    {:else if session}
                      Saved collection choices are unavailable right now. You can still search public and saved memes.
                    {:else}
                      Sign in to choose saved collections. You can still search public memes.
                    {/if}
                  </p>
                {/if}
                {#if collectionSelectionInvalid}
                  <p class="m-0 text-sm font-semibold text-danger" role={scopeValidationMessage ? 'alert' : 'status'}>
                    {scopeValidationMessage ?? 'Choose at least one collection before searching.'}
                  </p>
                {/if}
              </fieldset>
            </div>
          </div>

          <div class="sticky bottom-0 flex gap-3 border-t border-line bg-paper px-4 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
            <ActionLink href="/search" variant="secondary" class="flex-1">Reset</ActionLink>
            <Button form={formId} type="submit" class="flex-1" disabled={collectionSelectionInvalid}>Show results</Button>
          </div>
        </div>
      </Dialog.Content>
    </Dialog.Root>
  </div>
</form>

{#if nsfwRequestedButDisabled}
  <Notice class="mt-3">Sensitive results are requested in this link, but your account has not opted in yet. Choose Include sensitive content in Filters and confirm to see them.</Notice>
{/if}

<Dialog.Root bind:open={nsfwGateOpen}>
  <Dialog.Content role="alertdialog" aria-labelledby="nsfw-confirm-title" aria-describedby="nsfw-confirm-description">
    <Dialog.Title id="nsfw-confirm-title" class="m-0 text-2xl font-bold tracking-[-0.04em]">Include sensitive results?</Dialog.Title>
    <Dialog.Description id="nsfw-confirm-description" class="m-0 text-muted">
      This updates your account preference so searches can include sensitive memes when you choose them. You can change it later in Account.
    </Dialog.Description>
    {#if nsfwGateMessage}
      <p class="m-0 rounded-[14px] border border-danger/30 bg-danger/10 p-3 text-sm font-semibold text-danger" role="alert">{nsfwGateMessage}</p>
    {/if}
    <div class="flex flex-wrap justify-end gap-3">
      <Button type="button" variant="secondary" onclick={cancelNsfwOptIn} disabled={nsfwGatePending}>Cancel</Button>
      <Button type="button" onclick={confirmNsfwOptIn} disabled={nsfwGatePending}>{nsfwGatePending ? 'Saving...' : 'Confirm and search'}</Button>
    </div>
  </Dialog.Content>
</Dialog.Root>
