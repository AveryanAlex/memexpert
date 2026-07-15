<script lang="ts">
  import { Card, EmptyState } from '$lib/ui';
  import { finiteNonNegative, formatAnalyticsNumber } from './format';

  export interface AnalyticsFunnelData {
    searches: number;
    searches_with_results: number;
    searches_without_results: number;
    detail_clicks: number;
    downloads: number;
  }

  let { funnel }: { funnel: AnalyticsFunnelData } = $props();

  const stages = $derived([
    { label: 'Searches', value: finiteNonNegative(funnel.searches), detail: 'Initial non-empty searches' },
    { label: 'Results available', value: finiteNonNegative(funnel.searches_with_results), detail: 'Searches with one or more matches' },
    { label: 'Detail clicks', value: finiteNonNegative(funnel.detail_clicks), detail: 'Opened a meme from results' },
    { label: 'Downloads', value: finiteNonNegative(funnel.downloads), detail: 'Downloaded after discovery' }
  ]);
  const maxValue = $derived(Math.max(...stages.map((stage) => stage.value), 0));
  const noResults = $derived(finiteNonNegative(funnel.searches_without_results));
</script>

<Card class="grid gap-5" aria-labelledby="discovery-funnel-heading">
  <div class="grid gap-1">
    <p class="m-0 text-xs font-black uppercase tracking-[0.16em] text-muted">Discovery funnel</p>
    <h2 id="discovery-funnel-heading" class="m-0 text-2xl font-black tracking-[-0.04em]">From search to saved media</h2>
    <p class="m-0 text-sm text-muted">Counts are event signals, so later stages are not necessarily unique people.</p>
  </div>

  {#if maxValue === 0}
    <EmptyState title="No discovery activity yet" message="Search and downstream actions will appear here as people use the catalog." />
  {:else}
    <ol class="m-0 grid list-none gap-3 p-0" aria-label="Discovery funnel stages">
      {#each stages as stage (stage.label)}
        {@const percentage = Math.round((stage.value / maxValue) * 100)}
        {@const width = stage.value === 0 ? 0 : Math.max(22, percentage)}
        <li class="grid gap-2">
          <div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <div>
              <strong>{stage.label}</strong>
              <span class="ml-2 text-sm text-muted">{stage.detail}</span>
            </div>
            <span class="font-black tabular-nums">{formatAnalyticsNumber(stage.value)}</span>
          </div>
          <div class="h-10 overflow-hidden rounded-xl bg-soft" aria-hidden="true">
            <div class="grid h-full place-items-center rounded-xl bg-accent px-3 text-sm font-black text-on-accent transition-[width]" style:width={`${width}%`}>
              {percentage}%
            </div>
          </div>
        </li>
      {/each}
    </ol>
    <p class="m-0 rounded-2xl border border-line bg-soft px-4 py-3 text-sm text-muted">
      <strong class="text-ink">{formatAnalyticsNumber(noResults)} zero-result searches.</strong>
      Use the Engagement workspace to inspect the queries behind them.
    </p>
  {/if}
</Card>
