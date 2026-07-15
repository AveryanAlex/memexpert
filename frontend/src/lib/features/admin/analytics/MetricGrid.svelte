<script lang="ts">
  import type { AdminAnalyticsMetricRead } from '$lib/api/types';
  import { Card } from '$lib/ui';
  import {
    formatAnalyticsNumber,
    formatMetricChange,
    metricChangeTone,
    metricOrZero,
    type AnalyticsMetricFormat
  } from './format';

  export interface AnalyticsMetricDefinition {
    key: string;
    label: string;
    description: string;
    format?: AnalyticsMetricFormat;
    inverseChange?: boolean;
  }

  let {
    metrics,
    definitions
  }: {
    metrics: Record<string, AdminAnalyticsMetricRead>;
    definitions: AnalyticsMetricDefinition[];
  } = $props();
</script>

<section class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3" aria-label="Key metrics">
  {#each definitions as definition (definition.key)}
    {@const metric = metricOrZero(metrics, definition.key)}
    {@const tone = metricChangeTone(metric, definition.inverseChange ?? false)}
    <Card class="grid gap-3 p-5">
      <div class="flex items-start justify-between gap-3">
        <div class="grid gap-1">
          <h2 class="m-0 text-sm font-black uppercase tracking-[0.13em] text-muted">{definition.label}</h2>
          <p class="m-0 max-w-[22rem] text-sm text-muted">{definition.description}</p>
        </div>
        <span class="text-3xl font-black leading-none tracking-[-0.055em] text-ink">{formatAnalyticsNumber(metric.value, definition.format)}</span>
      </div>
      <p
        class={tone === 'positive' ? 'm-0 text-sm font-extrabold text-success-text' : tone === 'negative' ? 'm-0 text-sm font-extrabold text-danger' : 'm-0 text-sm font-extrabold text-muted'}
      >
        {formatMetricChange(metric, definition.format)}
      </p>
    </Card>
  {/each}
</section>
