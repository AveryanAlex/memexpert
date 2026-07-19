<script lang="ts">
  import { readAuthState } from '$lib/auth-state';
  import type { CurrentSessionRead, PublicMemeLandingRead } from '$lib/api/types';
  import MemeGrid from '$lib/features/memes/MemeGrid.svelte';
  import TrendAggregateHistory from '$lib/features/trends/TrendAggregateHistory.svelte';
  import { ActionLink, Badge, EmptyState, PageHeader } from '$lib/ui';

  type TaxonomyKind = 'tag' | 'template';

  interface TaxonomyLandingData {
    session?: CurrentSessionRead | null;
    sessionError?: string | null;
    landing: PublicMemeLandingRead | null;
    offset: number;
  }

  let { data, kind }: { data: TaxonomyLandingData; kind: TaxonomyKind } = $props();

  const authState = readAuthState(() => ({
    session: data.session ?? null,
    sessionError: data.sessionError ?? null
  }));
  const session = $derived($authState.session);
  const copy = $derived(
    kind === 'tag'
      ? {
          eyebrow: 'Tag',
          gridLabel: 'Tagged memes',
          emptyMessage: 'Try another tag or discover more memes.',
          aboutLabel: 'About this tag',
          popularityDescription: 'memes help shape this tag\'s recent popularity.',
          popularityLabel: 'Tag popularity summary',
          unavailableTitle: 'Tag unavailable',
          unavailableMessage: "We couldn't open this tag right now. Try discovering more memes."
        }
      : {
          eyebrow: 'Template',
          gridLabel: 'Template memes',
          emptyMessage: 'Try another template or discover more memes.',
          aboutLabel: 'About this template',
          popularityDescription: 'memes help shape this template\'s recent popularity.',
          popularityLabel: 'Template popularity summary',
          unavailableTitle: 'Template unavailable',
          unavailableMessage: "We couldn't open this template right now. Try discovering more memes."
        }
  );

  const page = $derived(data.landing?.page);
  const resultStart = $derived(page && page.total > 0 ? data.offset + 1 : 0);
  const resultEnd = $derived(page ? Math.min(data.offset + page.items.length, page.total) : 0);
  const previousOffset = $derived(page ? Math.max(data.offset - page.limit, 0) : 0);
  const nextOffset = $derived(page ? data.offset + page.limit : 0);
  const memes = $derived(page?.items.map((item) => item.meme) ?? []);
  const attributions = $derived(Object.fromEntries(page?.items.map((item) => [item.meme.id, item.attribution]) ?? []));
  const trendSummary = $derived(data.landing?.trend_summary ?? null);

  function pageHref(offset: number): string {
    return offset > 0 ? `?offset=${offset}` : '';
  }
</script>

{#if data.landing && page}
  <PageHeader title={data.landing.title} description={data.landing.description} eyebrow={copy.eyebrow} />

  <div class="mb-5 flex flex-wrap justify-between gap-3">
    <p class="m-0 text-muted">Showing {resultStart}-{resultEnd} of {page.total}</p>
    <a href="/search" class="text-muted">Search all memes</a>
  </div>

  {#if page.items.length > 0}
    <MemeGrid {memes} {attributions} label={copy.gridLabel} showAccessMarkers={Boolean(session)} />
  {:else}
    <EmptyState title="Nothing here yet" message={copy.emptyMessage}>
      <ActionLink href="/">Discover memes</ActionLink>
    </EmptyState>
  {/if}

  <nav class="mt-6 flex flex-wrap gap-2" aria-label="Pagination">
    {#if data.offset > 0}
      <ActionLink variant="secondary" href={pageHref(previousOffset)}>Previous</ActionLink>
    {/if}
    {#if page.has_more}
      <ActionLink href={pageHref(nextOffset)}>Next page</ActionLink>
    {/if}
  </nav>

  {#if trendSummary}
    <details class="mt-8 rounded-xl border border-line bg-paper">
      <summary class="cursor-pointer px-4 py-3 font-extrabold text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent">
        {copy.aboutLabel}
      </summary>
      <div class="grid gap-4 border-t border-line px-4 py-5">
        <p class="m-0 text-muted">{trendSummary.meme_count} {copy.popularityDescription}</p>
        <div class="flex flex-wrap gap-2" aria-label={copy.popularityLabel}>
          <Badge>{trendSummary.trend.recent.views} views</Badge>
          <Badge>{trendSummary.trend.recent.sends} sends</Badge>
          <Badge>{trendSummary.trend.recent.likes} likes</Badge>
          <Badge>{trendSummary.trend.recent.saves} saves</Badge>
        </div>
        {#if (trendSummary.points?.length ?? 0) >= 2}
          <TrendAggregateHistory summary={trendSummary} />
        {/if}
      </div>
    </details>
  {/if}
{:else}
  <EmptyState title={copy.unavailableTitle} message={copy.unavailableMessage}>
    <ActionLink href="/">Discover memes</ActionLink>
  </EmptyState>
{/if}
