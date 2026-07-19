<script lang="ts">
  import type { MemeSearchScope, WebCollectionListRead } from '$lib/api/types';
  import { Badge, PillLink } from '$lib/ui';
  import {
    buildSearchHref,
    LANGUAGE_OPTIONS,
    MEDIA_TYPE_OPTIONS,
    QUICK_SEARCH_TAGS,
    type SearchRouteState
  } from '$lib/searchParams';

  interface Props {
    filters: SearchRouteState;
    collections: WebCollectionListRead | null;
  }

  let { filters, collections }: Props = $props();

  const SUGGESTED_INTENTS = [
    { label: 'Cat reactions', query: 'cat reaction' },
    { label: 'Friday mood', query: 'friday mood' },
    { label: 'That meeting feeling', query: 'meeting reaction' }
  ];

  const scopeLabels: Record<MemeSearchScope, string> = {
    all: 'Everywhere',
    public: 'Public memes',
    private: 'My saved memes',
    collections: 'Specific collections'
  };
  const readableCollections = $derived(collections?.collections.filter((item) => item.capabilities.can_view) ?? []);
  const readableCollectionIds = $derived(new Set(readableCollections.map((item) => item.collection.id)));
  const selectedCollections = $derived(
    readableCollections.filter((item) => filters.scope === 'collections' && filters.collectionIds.includes(item.collection.id))
  );
  const unknownCollectionIds = $derived(
    filters.scope === 'collections' ? filters.collectionIds.filter((id) => !readableCollectionIds.has(id)) : []
  );
  const hasActiveFilters = $derived(
    filters.tags.length > 0 ||
      filters.includeNsfw ||
      Boolean(filters.mediaType) ||
      Boolean(filters.language) ||
      filters.scope !== 'public'
  );

  function filterHref(changes: Partial<SearchRouteState>): string {
    return buildSearchHref(filters, { ...changes, offset: 0 });
  }

  function removeCollectionHref(collectionId: string): string {
    const collectionIds = filters.collectionIds.filter((id) => id !== collectionId);
    return filterHref(collectionIds.length > 0 ? { collectionIds } : { scope: 'public', collectionIds: [] });
  }

  function optionLabel<T extends string>(options: Array<{ value: T; label: string }>, value: T | null): string {
    return value ? (options.find((option) => option.value === value)?.label ?? value) : '';
  }

  function categoryLabel(tag: string): string {
    return tag.charAt(0).toUpperCase() + tag.slice(1);
  }
</script>

{#if hasActiveFilters}
  <section class="mb-4 flex flex-wrap items-center gap-2" aria-label="Active filters">
    <span class="mr-1 text-sm font-semibold text-muted">Active filters</span>
    {#each filters.tags as tag}
      <a class="no-underline" href={filterHref({ tags: filters.tags.filter((item) => item !== tag) })} aria-label={`Remove ${tag} filter`}>
        <Badge>#{tag} <span aria-hidden="true">×</span></Badge>
      </a>
    {/each}
    {#if filters.mediaType}
      <a class="no-underline" href={filterHref({ mediaType: null })} aria-label={`Remove ${optionLabel(MEDIA_TYPE_OPTIONS, filters.mediaType)} filter`}>
        <Badge>{optionLabel(MEDIA_TYPE_OPTIONS, filters.mediaType)} <span aria-hidden="true">×</span></Badge>
      </a>
    {/if}
    {#if filters.language}
      <a class="no-underline" href={filterHref({ language: null })} aria-label={`Remove ${optionLabel(LANGUAGE_OPTIONS, filters.language)} filter`}>
        <Badge>{optionLabel(LANGUAGE_OPTIONS, filters.language)} <span aria-hidden="true">×</span></Badge>
      </a>
    {/if}
    {#if filters.includeNsfw}
      <a class="no-underline" href={filterHref({ includeNsfw: false })} aria-label="Remove sensitive content filter">
        <Badge>Sensitive content <span aria-hidden="true">×</span></Badge>
      </a>
    {/if}
    {#if filters.scope !== 'public'}
      <a class="no-underline" href={filterHref({ scope: 'public', collectionIds: [] })} aria-label={`Remove ${scopeLabels[filters.scope]} filter`}>
        <Badge>{scopeLabels[filters.scope]} <span aria-hidden="true">×</span></Badge>
      </a>
    {/if}
    {#each selectedCollections as item (item.collection.id)}
      <a
        class="no-underline"
        href={removeCollectionHref(item.collection.id)}
        aria-label={`Remove ${item.collection.title} filter`}
      >
        <Badge>{item.collection.title} <span aria-hidden="true">×</span></Badge>
      </a>
    {/each}
    {#each unknownCollectionIds as collectionId (collectionId)}
      <a
        class="no-underline"
        href={removeCollectionHref(collectionId)}
        aria-label="Remove selected collection filter"
      >
        <Badge>Selected collection <span aria-hidden="true">×</span></Badge>
      </a>
    {/each}
  </section>
{:else if !filters.query}
  <section class="mb-5 grid gap-3" aria-labelledby="search-ideas-title">
    <div>
      <h2 id="search-ideas-title" class="m-0 text-sm font-semibold text-muted">Try a search</h2>
      <div class="mt-2 flex flex-wrap gap-2">
        {#each SUGGESTED_INTENTS as intent}
          <PillLink size="compact" href={filterHref({ query: intent.query })}>
            {intent.label}
          </PillLink>
        {/each}
      </div>
    </div>
    <div>
      <h2 class="m-0 text-sm font-semibold text-muted">Browse categories</h2>
      <div class="mt-2 flex flex-wrap gap-2" aria-label="Quick categories">
        {#each QUICK_SEARCH_TAGS as tag}
          <PillLink size="compact" href={filterHref({ tags: [tag] })}>
            #{categoryLabel(tag)}
          </PillLink>
        {/each}
      </div>
    </div>
  </section>
{/if}
