<script lang="ts">
  import MemeCard from '$lib/features/memes/MemeCard.svelte';
  import { trendTimelineHref } from '$lib/features/trends/params';
  import { ActionLink, Card, EmptyState, Notice, PageHeader } from '$lib/ui';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  const previousOffset = $derived(Math.max(data.offset - data.timeline.limit, 0));
  const nextOffset = $derived(data.offset + data.timeline.limit);

  function periodLabel(raw: string, granularity: 'month' | 'year' | string): string {
    const date = new Date(raw);
    if (granularity === 'year') {
      return new Intl.DateTimeFormat('en', { year: 'numeric' }).format(date);
    }
    return new Intl.DateTimeFormat('en', { month: 'long', year: 'numeric' }).format(date);
  }
</script>

<PageHeader
  title="Meme timeline."
  description="Browse months or years with derived source-engagement and platform-event points. Period rankings use real deltas and event counts only."
  badge="Real engagement"
>
  <ActionLink href="/trends" variant="secondary">Back to trends</ActionLink>
  <ActionLink href="/trends/compare" variant="secondary">Compare trends</ActionLink>
</PageHeader>

<nav class="mb-6 flex flex-wrap gap-2" aria-label="Timeline granularity">
  <a class={data.granularity === 'month' ? 'rounded-full bg-ink px-4 py-3 font-extrabold text-paper no-underline' : 'rounded-full border border-line bg-paper px-4 py-3 font-extrabold text-ink no-underline'} href={trendTimelineHref('month')}>By month</a>
  <a class={data.granularity === 'year' ? 'rounded-full bg-ink px-4 py-3 font-extrabold text-paper no-underline' : 'rounded-full border border-line bg-paper px-4 py-3 font-extrabold text-ink no-underline'} href={trendTimelineHref('year')}>By year</a>
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
            <p class="m-0 text-muted">{period.meme_count} memes · {period.snapshot_count} source checks</p>
          </div>
          <span class="rounded-full border border-line bg-soft px-3 py-2 text-sm font-extrabold">{period.period}</span>
        </div>

        {#if period.top_memes.length > 0}
          <div class="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {#each period.top_memes as item (`${period.period}:${item.meme.id}`)}
              <div class="grid gap-3 rounded-[28px] border border-line bg-cream/60 p-3">
                <MemeCard meme={item.meme} />
                <dl class="m-0 grid grid-cols-2 gap-2 text-sm">
                  <div class="rounded-[16px] bg-paper p-3">
                    <dt class="text-muted">Popularity</dt>
                    <dd class="m-0 font-extrabold">{item.popularity_score.toFixed(1)}</dd>
                  </div>
                  <div class="rounded-[16px] bg-paper p-3">
                    <dt class="text-muted">Snapshots</dt>
                    <dd class="m-0 font-extrabold">{item.snapshot_count}</dd>
                  </div>
                  <div class="rounded-[16px] bg-paper p-3">
                    <dt class="text-muted">Source views</dt>
                    <dd class="m-0 font-extrabold">{item.source_views}</dd>
                  </div>
                  <div class="rounded-[16px] bg-paper p-3">
                    <dt class="text-muted">Platform views</dt>
                    <dd class="m-0 font-extrabold">{item.platform_views}</dd>
                  </div>
                </dl>
              </div>
            {/each}
          </div>
        {:else}
          <p class="m-0 text-muted">This period has snapshot totals but no visible public meme cards.</p>
        {/if}
      </Card>
    {/each}
  </section>
{:else if !data.errorMessage}
  <EmptyState title="No timeline data yet" message="No public engagement points are available. The timeline only appears after source deltas or platform events have been captured." />
{/if}

<nav class="mt-6 flex flex-wrap gap-2" aria-label="Timeline pagination">
  {#if data.offset > 0}
    <ActionLink variant="secondary" href={trendTimelineHref(data.granularity, previousOffset)}>Previous</ActionLink>
  {/if}
  {#if data.timeline.has_more}
    <ActionLink href={trendTimelineHref(data.granularity, nextOffset)}>Next periods</ActionLink>
  {/if}
</nav>
