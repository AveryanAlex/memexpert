<script lang="ts">
  import { goto } from '$app/navigation';
  import { navigating } from '$app/state';
  import { ApiError, updateUserPreferences } from '$lib/api/client';
  import type { ContentKind, ContentLanguage } from '$lib/api/types';
  import { bulkGuidanceFromSessionAndCollections, collectionListBulkOptions } from '$lib/features/memes/bulk-view-model';
  import InfiniteMemeFeed from '$lib/features/memes/InfiniteMemeFeed.svelte';
  import { ActionLink, Badge, Button, Card, FormRow, Input, LoadingState, Notice, PageHeader, Select } from '$lib/ui';
  import * as Dialog from '$lib/ui/dialog';
  import { buildSearchHref, LANGUAGE_OPTIONS, MEDIA_TYPE_OPTIONS, normalizeTags, QUICK_SEARCH_TAGS, type SearchRouteState } from '$lib/searchParams';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  let searchForm = $state<HTMLFormElement>();
  let nsfwGateOpen = $state(false);
  let pendingSearchHref = $state<string | null>(null);
  let nsfwGatePending = $state(false);
  let nsfwGateMessage = $state<string | null>(null);

  const bulkOptions = $derived(collectionListBulkOptions(data.collections));
  const bulkGuidance = $derived(bulkGuidanceFromSessionAndCollections(data.session ?? null, bulkOptions));
  const loadingSearch = $derived(navigating.to?.url.pathname === '/search');
  const shouldConfirmNsfw = $derived(data.session?.user.nsfw_enabled === false);
  const nsfwRequestedButDisabled = $derived(data.filters.includeNsfw && data.session?.user.nsfw_enabled !== true);
  const activeFilterCount = $derived(
    data.filters.tags.length +
      (data.filters.includeNsfw ? 1 : 0) +
      (data.filters.mediaType ? 1 : 0) +
      (data.filters.language ? 1 : 0)
  );

  function tagHref(tag: string): string {
    const selected = data.filters.tags.includes(tag);
    const tags = selected ? data.filters.tags.filter((item) => item !== tag) : [...data.filters.tags, tag];
    return buildSearchHref(data.filters, { tags, offset: 0 });
  }

  function removeTagHref(tag: string): string {
    return buildSearchHref(data.filters, { tags: data.filters.tags.filter((item) => item !== tag), offset: 0 });
  }

  function handleSearchSubmit(event: SubmitEvent) {
    const form = event.currentTarget as HTMLFormElement;
    const nextFilters = searchStateFromForm(form);
    if (!nextFilters.includeNsfw || !shouldConfirmNsfw) {
      return;
    }

    event.preventDefault();
    pendingSearchHref = buildSearchHref(nextFilters, { offset: 0 });
    nsfwGateMessage = null;
    nsfwGateOpen = true;
  }

  function cancelNsfwOptIn() {
    nsfwGateOpen = false;
    pendingSearchHref = null;
    nsfwGateMessage = null;
    const nsfwSelect = searchForm?.elements.namedItem('include_nsfw');
    if (nsfwSelect instanceof HTMLSelectElement) {
      nsfwSelect.value = 'false';
    }
  }

  async function confirmNsfwOptIn() {
    if (!pendingSearchHref) {
      return;
    }

    nsfwGatePending = true;
    nsfwGateMessage = null;
    try {
      await updateUserPreferences({ fetch, body: { nsfw_enabled: true } });
      const href = pendingSearchHref;
      nsfwGateOpen = false;
      pendingSearchHref = null;
      await goto(href, { invalidateAll: true });
    } catch (error) {
      nsfwGateMessage = error instanceof ApiError || error instanceof Error ? error.message : 'Could not update NSFW preference.';
    } finally {
      nsfwGatePending = false;
    }
  }

  function searchStateFromForm(form: HTMLFormElement): SearchRouteState {
    const formData = new FormData(form);
    const rawMediaType = String(formData.get('media_type') ?? '');
    const rawLanguage = String(formData.get('language') ?? '');

    return {
      query: String(formData.get('q') ?? '').trim(),
      tags: normalizeTags([String(formData.get('tags') ?? '')]),
      includeNsfw: String(formData.get('include_nsfw') ?? 'false') === 'true',
      mediaType: MEDIA_TYPE_OPTIONS.some((option) => option.value === rawMediaType) ? (rawMediaType as ContentKind) : null,
      language: LANGUAGE_OPTIONS.some((option) => option.value === rawLanguage) ? (rawLanguage as ContentLanguage) : null,
      offset: 0
    };
  }
</script>

<PageHeader title="Search MemeXpert." description="Find a meme by phrase, tag, format, language, and safe-content preferences. Every filter lives in the URL so results are shareable." badge="Public catalog" />

<Card class="mb-6 grid gap-5" aria-labelledby="search-filters-title">
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div>
      <h2 id="search-filters-title" class="m-0 text-2xl font-black tracking-[-0.04em]">Discovery filters</h2>
      <p class="m-0 text-muted">Use comma-separated tags or tap a category below.</p>
    </div>
    {#if activeFilterCount > 0 || data.filters.query}
      <ActionLink variant="ghost" size="compact" href="/search">Clear all</ActionLink>
    {/if}
  </div>

  <form bind:this={searchForm} class="grid gap-3 md:grid-cols-[minmax(16rem,1.2fr)_minmax(12rem,0.9fr)_minmax(10rem,0.7fr)_minmax(10rem,0.7fr)_auto]" method="GET" action="/search" onsubmit={handleSearchSubmit}>
    <FormRow label="Search text" class="md:col-span-2">
      <Input name="q" type="search" placeholder="try: cat reaction, friday mood" value={data.filters.query} />
    </FormRow>
    <FormRow label="Media type">
      <Select name="media_type" value={data.filters.mediaType ?? ''}>
        <option value="">Any type</option>
        {#each MEDIA_TYPE_OPTIONS as option}
          <option value={option.value}>{option.label}</option>
        {/each}
      </Select>
    </FormRow>
    <FormRow label="Language">
      <Select name="language" value={data.filters.language ?? ''}>
        <option value="">Any language</option>
        {#each LANGUAGE_OPTIONS as option}
          <option value={option.value}>{option.label}</option>
        {/each}
      </Select>
    </FormRow>
    <FormRow label="NSFW">
      <Select name="include_nsfw" value={String(data.filters.includeNsfw)}>
        <option value="false">Hide NSFW</option>
        <option value="true">Include NSFW</option>
      </Select>
    </FormRow>
    <FormRow label="Tags / categories" class="md:col-span-4">
      <Input name="tags" placeholder="reaction, wholesome, work" value={data.filters.tags.join(', ')} />
    </FormRow>
    <div class="flex items-end">
      <Button class="w-full" type="submit">Search</Button>
    </div>
  </form>

  <div class="flex flex-wrap gap-2" aria-label="Quick category filters">
    {#each QUICK_SEARCH_TAGS as tag}
      <a
        class={data.filters.tags.includes(tag) ? 'rounded-full bg-ink px-4 py-2 text-sm font-extrabold text-paper no-underline' : 'rounded-full border border-line bg-paper px-4 py-2 text-sm font-extrabold text-ink no-underline hover:bg-soft'}
        href={tagHref(tag)}
        aria-label={data.filters.tags.includes(tag) ? `Remove ${tag} category filter` : `Add ${tag} category filter`}
      >#{tag}</a>
    {/each}
  </div>
</Card>

{#if nsfwRequestedButDisabled}
  <Notice>NSFW was requested in the URL, but this session has not opted in. Results remain filtered until you choose Include NSFW from Search and confirm the account preference.</Notice>
{/if}

<Dialog.Root bind:open={nsfwGateOpen}>
  <Dialog.Content role="alertdialog" aria-labelledby="nsfw-confirm-title" aria-describedby="nsfw-confirm-description">
    <Dialog.Title id="nsfw-confirm-title" class="m-0 text-2xl font-black tracking-[-0.04em]">Include NSFW results?</Dialog.Title>
    <Dialog.Description id="nsfw-confirm-description" class="m-0 text-muted">
      This changes your account preference so searches can include NSFW memes when the URL filter asks for them. You can turn it back off from Profile.
    </Dialog.Description>
    {#if nsfwGateMessage}
      <p class="m-0 rounded-[18px] border border-danger/30 bg-danger/10 p-3 text-sm font-extrabold text-danger" role="alert">{nsfwGateMessage}</p>
    {/if}
    <div class="flex flex-wrap justify-end gap-3">
      <Button type="button" variant="secondary" onclick={cancelNsfwOptIn} disabled={nsfwGatePending}>Cancel</Button>
      <Button type="button" onclick={confirmNsfwOptIn} disabled={nsfwGatePending}>{nsfwGatePending ? 'Saving...' : 'Confirm and search'}</Button>
    </div>
  </Dialog.Content>
</Dialog.Root>

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
    language: data.filters.language
  }}
  initialError={data.errorMessage}
  label="Search results"
  emptyMessage="Try a shorter phrase, remove a tag, or broaden media and language filters."
  bulk={{ enabled: true, saveEnabled: true, collectionOptions: bulkOptions, guidance: bulkGuidance }}
>
  {#snippet summary()}
    {#if data.filters.query}
      <p class="m-0 text-muted">Results for “{data.filters.query}”</p>
    {:else}
      <p class="m-0 text-muted">Browsing public memes</p>
    {/if}
    {#each data.filters.tags as tag}
      <a class="no-underline" href={removeTagHref(tag)} aria-label={`Remove ${tag} filter`}><Badge>#{tag} x</Badge></a>
    {/each}
    {#if data.filters.mediaType}<Badge>{data.filters.mediaType}</Badge>{/if}
    {#if data.filters.language}<Badge>{data.filters.language}</Badge>{/if}
    {#if data.filters.includeNsfw}<Badge>NSFW included</Badge>{/if}
  {/snippet}
  {#snippet emptyAction()}
    {#if activeFilterCount > 0 || data.filters.query}
      <ActionLink href="/search">Browse everything</ActionLink>
    {/if}
  {/snippet}
</InfiniteMemeFeed>
