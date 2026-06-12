<script lang="ts">
  import { navigating } from '$app/state';
  import MemeGrid from '$lib/features/memes/MemeGrid.svelte';
  import { ActionLink, Badge, Button, Card, EmptyState, FormRow, Input, LoadingState, Notice, PageHeader, Select } from '$lib/ui';
  import { buildSearchHref, LANGUAGE_OPTIONS, MEDIA_TYPE_OPTIONS, QUICK_SEARCH_TAGS } from '$lib/searchParams';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  const resultStart = $derived(data.page.total === 0 ? 0 : data.filters.offset + 1);
  const resultEnd = $derived(Math.min(data.filters.offset + data.page.items.length, data.page.total));
  const previousOffset = $derived(Math.max(data.filters.offset - data.page.limit, 0));
  const nextOffset = $derived(data.filters.offset + data.page.limit);
  const memes = $derived(data.page.items.map((item) => item.meme));
  const loadingSearch = $derived(navigating.to?.url.pathname === '/search');
  const activeFilterCount = $derived(
    data.filters.tags.length +
      (data.filters.includeNsfw ? 1 : 0) +
      (data.filters.mediaType ? 1 : 0) +
      (data.filters.language ? 1 : 0)
  );

  function pageHref(offset: number): string {
    return buildSearchHref(data.filters, { offset });
  }

  function tagHref(tag: string): string {
    const selected = data.filters.tags.includes(tag);
    const tags = selected ? data.filters.tags.filter((item) => item !== tag) : [...data.filters.tags, tag];
    return buildSearchHref(data.filters, { tags, offset: 0 });
  }

  function removeTagHref(tag: string): string {
    return buildSearchHref(data.filters, { tags: data.filters.tags.filter((item) => item !== tag), offset: 0 });
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

  <form class="grid gap-3 md:grid-cols-[minmax(16rem,1.2fr)_minmax(12rem,0.9fr)_minmax(10rem,0.7fr)_minmax(10rem,0.7fr)_auto]" method="GET" action="/search">
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

{#if data.errorMessage}
  <Notice tone="danger" role="alert">{data.errorMessage}</Notice>
{/if}

{#if loadingSearch}
  <LoadingState label="Loading filtered results" />
{/if}

<div class="my-7 flex flex-wrap items-center justify-between gap-3">
  <div class="flex flex-wrap items-center gap-2">
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
  </div>
  <p class="m-0 text-muted">Showing {resultStart}-{resultEnd} of {data.page.total}</p>
</div>

{#if data.page.items.length > 0}
  <MemeGrid {memes} label="Search results" />
{:else if !data.errorMessage}
  <EmptyState title="No memes found" message="Try a shorter phrase, remove a tag, or broaden media and language filters.">
    {#if activeFilterCount > 0 || data.filters.query}
      <ActionLink href="/search">Browse everything</ActionLink>
    {/if}
  </EmptyState>
{/if}

<nav class="mt-6 flex flex-wrap gap-2" aria-label="Pagination">
  {#if data.filters.offset > 0}
    <ActionLink variant="secondary" href={pageHref(previousOffset)}>Previous</ActionLink>
  {/if}
  {#if data.page.has_more}
    <ActionLink href={pageHref(nextOffset)}>Next page</ActionLink>
  {/if}
</nav>
