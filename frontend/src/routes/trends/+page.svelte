<script lang="ts">
  import { readAuthState } from '$lib/auth-state';
  import MemeCard from '$lib/features/memes/MemeCard.svelte';
  import TrendSummary from '$lib/features/trends/TrendSummary.svelte';
  import type { PublicTrendMetricsRead, PublicTrendSummaryRead } from '$lib/api/types';
  import { ActionLink, Card, EmptyState, Notice, PageHeader, PillLink } from '$lib/ui';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  const authState = readAuthState(() => ({ session: data.session ?? null, sessionError: data.sessionError }));
  const session = $derived($authState.session);

  const resultStart = $derived(data.page.total === 0 ? 0 : data.offset + 1);
  const resultEnd = $derived(Math.min(data.offset + data.page.items.length, data.page.total));
  const previousOffset = $derived(Math.max(data.offset - data.page.limit, 0));
  const nextOffset = $derived(data.offset + data.page.limit);
  const rankingLabel = $derived(rankingName(data.ranking));
  const numberFormatter = new Intl.NumberFormat('en');
  const recordedActivityDescription =
    'Recorded activity adds original-source views, reactions, and reposts to MemeExpert views, sends, saves, and favorites. It counts signals, not unique people.';
  const rankings = [
    { value: 'trending', label: 'Trending' },
    { value: 'fastest_rising', label: 'Rising' },
    { value: 'most_liked', label: 'Most favorited' }
  ] as const;

  function rankingHref(ranking: string, offset = 0): string {
    const params = new URLSearchParams({ ranking });
    if (offset > 0) {
      params.set('offset', String(offset));
    }
    return `/trends?${params.toString()}`;
  }

  function rankingName(ranking: string): string {
    if (ranking === 'fastest_rising') return 'Rising';
    if (ranking === 'most_liked') return 'Most favorited';
    return 'Trending';
  }

  function summaryStory(summary: PublicTrendSummaryRead): string {
    return `Latest recorded activity: ${formatCount(recordedActivity(summary.trend))} signals`;
  }

  function memeCountLabel(count: number): string {
    return `${count} ${count === 1 ? 'meme' : 'memes'}`;
  }

  function recordedActivity(trend: PublicTrendMetricsRead): number {
    return (
      count(trend.latest_source_views) +
      count(trend.latest_source_reactions) +
      count(trend.latest_source_reposts) +
      count(trend.latest_platform_views) +
      count(trend.latest_platform_sends) +
      count(trend.latest_platform_saves) +
      count(trend.latest_platform_likes)
    );
  }

  function formatCount(value: number): string {
    return numberFormatter.format(value);
  }

  function count(value: number | null | undefined): number {
    return typeof value === 'number' && Number.isFinite(value) ? Math.max(value, 0) : 0;
  }
</script>

<PageHeader title="Meme trends" description="See what people are enjoying this week." badge="This week">
  <ActionLink href="/trends/compare" variant="secondary">Compare</ActionLink>
  <ActionLink href="/trends/timeline" variant="secondary">Browse by time</ActionLink>
</PageHeader>

<p class="mb-6 max-w-3xl text-sm text-muted">{recordedActivityDescription}</p>

<nav class="mb-6 flex flex-wrap gap-2" aria-label="Trend rankings">
  {#each rankings as ranking}
    <PillLink active={data.ranking === ranking.value} href={rankingHref(ranking.value)}>{ranking.label}</PillLink>
  {/each}
</nav>

{#if data.errorMessage}
  <Notice>{data.errorMessage}</Notice>
{/if}

<div class="my-7 flex flex-wrap justify-between gap-3">
  <p class="m-0 text-muted">Showing {resultStart}-{resultEnd} of {data.page.total}</p>
  <a href="/" class="text-muted">Discover memes</a>
</div>

{#if data.page.items.length > 0}
  <section class="grid grid-cols-1 gap-4 md:grid-cols-3" aria-label="Trend ranked memes">
    {#each data.page.items as item, index (item.meme.id)}
      <Card class="grid gap-3 p-4 shadow-none">
        <div class="flex items-center justify-between gap-3">
          <span class="rounded-full bg-soft px-3 py-1.5 text-sm font-extrabold text-ink">{rankingLabel}</span>
          <span class="text-sm font-semibold text-muted">#{data.offset + index + 1}</span>
        </div>
        <MemeCard
          meme={item.meme}
          attribution={item.attribution}
          exposureId={item.attribution.impression_id}
          exposurePlacement={`trends:${data.ranking}:${data.offset + index}:${item.meme.id}`}
          showAccessMarkers={Boolean(session)}
        />
        <TrendSummary trend={item.trend} />
      </Card>
    {/each}
  </section>
{:else if !data.errorMessage}
  <EmptyState title="Nothing is trending yet" message="Check back soon for the memes people are starting to enjoy." />
{/if}

<nav class="mt-6 flex flex-wrap gap-2" aria-label="Pagination">
  {#if data.offset > 0}
    <ActionLink variant="secondary" href={rankingHref(data.ranking, previousOffset)}>Previous</ActionLink>
  {/if}
  {#if data.page.has_more}
    <ActionLink href={rankingHref(data.ranking, nextOffset)}>Next page</ActionLink>
  {/if}
</nav>

<section class="mt-7 grid gap-4 md:grid-cols-2" aria-label="Popular tags and templates">
  <Card class="grid gap-3 shadow-none">
    <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Tags people are enjoying</h2>
    {#if data.tagSummaries.length > 0}
      {#each data.tagSummaries as summary}
        <a class="flex items-center justify-between gap-3 rounded-[18px] border border-line bg-paper px-4 py-3 font-extrabold no-underline" href={`/tags/${summary.slug}`}>
          <span>{summary.title}</span>
          <small class="text-right text-muted">{memeCountLabel(summary.meme_count)} · {summaryStory(summary)}</small>
        </a>
      {/each}
    {:else}
      <p class="m-0 text-muted">No tags are standing out just yet.</p>
    {/if}
  </Card>
  <Card class="grid gap-3 shadow-none">
    <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Templates people are enjoying</h2>
    {#if data.templateSummaries.length > 0}
      {#each data.templateSummaries as summary}
        <a class="flex items-center justify-between gap-3 rounded-[18px] border border-line bg-paper px-4 py-3 font-extrabold no-underline" href={`/templates/${summary.slug}`}>
          <span>{summary.title}</span>
          <small class="text-right text-muted">{memeCountLabel(summary.meme_count)} · {summaryStory(summary)}</small>
        </a>
      {/each}
    {:else}
      <p class="m-0 text-muted">No templates are standing out just yet.</p>
    {/if}
  </Card>
</section>
