<script lang="ts">
  import { readAuthState } from '$lib/auth-state';
  import MemeCard from '$lib/features/memes/MemeCard.svelte';
  import type { MemeVideoPreviewMode } from '$lib/features/memes/meme-video';
  import type { PublicTrendTimelineMemeRead } from '$lib/api/types';
  import { trendTimelineHref } from '$lib/features/trends/params';
  import { ActionLink, Card, EmptyState, Masonry, Notice, PageHeader, PillLink } from '$lib/ui';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  const authState = readAuthState(() => ({ session: data.session ?? null, sessionError: data.sessionError }));
  const session = $derived($authState.session);

  const previousOffset = $derived(Math.max(data.offset - data.timeline.limit, 0));
  const nextOffset = $derived(data.offset + data.timeline.limit);
  const numberFormatter = new Intl.NumberFormat('en');
  const recordedActivityDescription =
    'Recorded activity adds original-source views, reactions, and reposts to MemeExpert views, sends, saves, and favorites. It counts signals, not unique people.';
  const granularities = [
    { value: 'month', label: 'By month' },
    { value: 'year', label: 'By year' }
  ] as const;

  function periodLabel(raw: string, granularity: 'month' | 'year' | string): string {
    const date = new Date(raw);
    if (granularity === 'year') {
      return new Intl.DateTimeFormat('en', { year: 'numeric', timeZone: 'UTC' }).format(date);
    }
    return new Intl.DateTimeFormat('en', { month: 'long', year: 'numeric', timeZone: 'UTC' }).format(date);
  }

  function periodSummary(memeCount: number): string {
    return `${formatCount(memeCount)} ${memeCount === 1 ? 'top meme' : 'top memes'} to revisit`;
  }

  function activitySummary(item: PublicTrendTimelineMemeRead): string {
    return `${formatCount(recordedActivity(item))} signals`;
  }

  function activityBreakdown(item: PublicTrendTimelineMemeRead): string {
    return `Original sources: ${formatCount(sourceActivity(item))} · MemeExpert: ${formatCount(memeExpertActivity(item))}`;
  }

  /** Counts all source and MemeExpert signals without weighting or deduplication. */
  function recordedActivity(item: PublicTrendTimelineMemeRead): number {
    return sourceActivity(item) + memeExpertActivity(item);
  }

  function sourceActivity(item: PublicTrendTimelineMemeRead): number {
    return count(item.source_views) + count(item.source_reactions) + count(item.source_reposts);
  }

  function memeExpertActivity(item: PublicTrendTimelineMemeRead): number {
    return count(item.platform_views) + count(item.platform_sends) + count(item.platform_saves) + count(item.platform_likes);
  }

  function formatCount(value: number): string {
    return numberFormatter.format(value);
  }

  function count(value: number | null | undefined): number {
    return typeof value === 'number' && Number.isFinite(value) ? Math.max(value, 0) : 0;
  }

  function timelineMemeKey(item: PublicTrendTimelineMemeRead): string {
    return item.meme.id;
  }

  function cardVideoPreviewMode(columnCount: number, ready: boolean): MemeVideoPreviewMode {
    if (!ready || columnCount === 0) return 'poster';
    return columnCount === 1 ? 'viewport' : 'hover';
  }
</script>

<PageHeader
  title="Meme timeline."
  description="Take a trip through the memes people loved, month by month or year by year."
  badge="Look back"
>
  <ActionLink href="/trends" variant="secondary">Back to trends</ActionLink>
  <ActionLink href="/trends/compare" variant="secondary">Compare</ActionLink>
</PageHeader>

<p class="mb-6 max-w-3xl text-sm text-muted">{recordedActivityDescription}</p>

<nav class="mb-6 flex flex-wrap gap-2" aria-label="Timeline granularity">
  {#each granularities as granularity}
    <PillLink
      active={data.granularity === granularity.value}
      href={trendTimelineHref(granularity.value)}
    >{granularity.label}</PillLink>
  {/each}
</nav>

{#if data.errorMessage}
  <Notice>{data.errorMessage}</Notice>
{/if}

{#if data.timeline.periods.length > 0}
  <section class="grid gap-6" aria-label="Timeline periods">
    {#each data.timeline.periods as period (period.period)}
      <Card class="grid gap-4 shadow-none">
        <div class="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 class="m-0 text-3xl font-black tracking-[-0.05em]">{periodLabel(period.period_start, data.granularity)}</h2>
            <p class="m-0 text-muted">{periodSummary(period.meme_count)}</p>
          </div>
        </div>

        {#if period.top_memes.length > 0}
          <Masonry
            items={period.top_memes}
            getKey={timelineMemeKey}
            maxColumns={3}
            element="section"
            aria-label={`Top memes from ${periodLabel(period.period_start, data.granularity)}`}
            role="list"
          >
            {#snippet children(item, index, layout)}
              <div
                class="grid gap-3 rounded-xl border border-line bg-cream/60 p-3"
                role="listitem"
                aria-posinset={index + 1}
                aria-setsize={period.top_memes.length}
              >
                <MemeCard
                  meme={item.meme}
                  exposurePlacement={`timeline:${period.period}:${item.meme.id}`}
                  showAccessMarkers={Boolean(session)}
                  showZoom={layout.ready && layout.columnCount > 1}
                  videoPreviewMode={cardVideoPreviewMode(layout.columnCount, layout.ready)}
                />
                <div class="grid gap-1 text-sm">
                  <p class="m-0 font-semibold text-ink">Recorded activity · {activitySummary(item)}</p>
                  <p class="m-0 text-muted">{activityBreakdown(item)}</p>
                </div>
              </div>
            {/snippet}
          </Masonry>
        {:else}
          <p class="m-0 text-muted">No public memes from this time are ready to revisit yet.</p>
        {/if}
      </Card>
    {/each}
  </section>
{:else if !data.errorMessage}
  <EmptyState title="No moments to revisit yet" message="Come back soon to look back at emerging favorites." />
{/if}

<nav class="mt-6 flex flex-wrap gap-2" aria-label="Timeline pagination">
  {#if data.offset > 0}
    <ActionLink variant="secondary" href={trendTimelineHref(data.granularity, previousOffset)}>Previous</ActionLink>
  {/if}
  {#if data.timeline.has_more}
    <ActionLink href={trendTimelineHref(data.granularity, nextOffset)}>Next periods</ActionLink>
  {/if}
</nav>
