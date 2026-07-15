<script lang="ts">
  import type { AdminAnalyticsAudienceRead, AdminAnalyticsRetentionPeriodRead } from '$lib/api/types';
  import { Card, EmptyState, Notice } from '$lib/ui';
  import AnalyticsDonut from './AnalyticsDonut.svelte';
  import AnalyticsHeader from './AnalyticsHeader.svelte';
  import MetricGrid, { type AnalyticsMetricDefinition } from './MetricGrid.svelte';
  import AnalyticsTimeSeriesChart from './AnalyticsTimeSeriesChart.svelte';
  import { formatAnalyticsNumber } from './format';
  import { analyticsRangeFromRead, formatUtcDate, type AdminAnalyticsRangeParams } from './range';

  let {
    dashboard,
    requestedRange,
    loadError
  }: {
    dashboard: AdminAnalyticsAudienceRead | null;
    requestedRange: AdminAnalyticsRangeParams;
    loadError: string | null;
  } = $props();

  const range = $derived(analyticsRangeFromRead(dashboard?.range));
  const metricDefinitions: AnalyticsMetricDefinition[] = [
    { key: 'new_guests', label: 'New guests', description: 'Guest accounts created during the selected range.' },
    { key: 'new_full_accounts', label: 'New full accounts', description: 'Full accounts created or upgraded in the selected range.' },
    { key: 'active_users', label: 'Active users', description: 'Accounts with a recorded product event.' },
    { key: 'active_guests', label: 'Active guests', description: 'Guest accounts with a recorded product event.' },
    { key: 'active_full_accounts', label: 'Active full accounts', description: 'Full accounts with a recorded product event.' },
    { key: 'guest_to_full_conversions', label: 'Guest → full', description: 'Recorded guest account upgrades or merges.' },
    { key: 'guest_to_full_conversion_rate', label: 'Conversion rate', description: 'Guest-to-full conversions relative to new guests.', format: 'percent' }
  ];
  const activity = $derived(
    (dashboard?.activity ?? []).map((point) => ({
      date: point.date,
      values: {
        new_guests: point.new_guests,
        new_full_accounts: point.new_full_accounts,
        active_users: point.active_users,
        guest_to_full_conversions: point.guest_to_full_conversions
      }
    }))
  );

  function retentionLabel(period: AdminAnalyticsRetentionPeriodRead | null): string {
    if (!period) return 'Not mature yet';
    const rate = period.rate === null ? 'Not available' : formatAnalyticsNumber(period.rate, 'percent');
    return `${rate} · ${formatAnalyticsNumber(period.retained_users)}/${formatAnalyticsNumber(period.eligible_users)} retained`;
  }
</script>

<AnalyticsHeader
  activeSection="audience"
  currentPath="/admin/analytics/audience"
  title="Audience & retention"
  description="Track account growth, active account mix, conversion, and mature D1/D7/D30 cohorts without exposing individual users."
  {range}
  {requestedRange}
/>

{#if loadError}
  <Notice tone="danger" role="alert">{loadError}</Notice>
{/if}

{#if dashboard}
  <MetricGrid metrics={dashboard.metrics} definitions={metricDefinitions} />

  <div class="mt-7 grid gap-6 2xl:grid-cols-[minmax(0,1.55fr)_minmax(20rem,0.85fr)]">
    <AnalyticsTimeSeriesChart
      label="Audience growth and activity"
      description="New guest and full accounts, active accounts, and guest-to-full conversion events by UTC date."
      points={activity}
      series={[
        { key: 'new_guests', label: 'New guests', color: '#0369a1' },
        { key: 'new_full_accounts', label: 'New full accounts', color: '#047857' },
        { key: 'active_users', label: 'Active users', color: '#7c3aed' },
        { key: 'guest_to_full_conversions', label: 'Conversions', color: '#b45309' }
      ]}
    />
    <AnalyticsDonut items={dashboard.surface_mix} label="Audience activity by surface" />
  </div>

  <Card class="mt-7 grid gap-4" aria-labelledby="retention-heading">
    <div class="grid gap-1">
      <p class="m-0 text-xs font-black uppercase tracking-[0.16em] text-muted">Cohort retention</p>
      <h2 id="retention-heading" class="m-0 text-3xl font-black tracking-[-0.05em]">Mature account cohorts</h2>
      <p class="m-0 max-w-3xl text-sm text-muted">Each row groups accounts created on the same UTC date. A retention period is shown only after it has had enough time to mature.</p>
    </div>
    {#if dashboard.retention_cohorts.length > 0}
      <div class="overflow-x-auto rounded-2xl border border-line">
        <table class="w-full min-w-[55rem] border-collapse text-left text-sm">
          <caption class="sr-only">Account retention cohorts with D1, D7, and D30 retention.</caption>
          <thead class="bg-soft text-muted"><tr><th class="px-4 py-3 font-black">Cohort date</th><th class="px-4 py-3 font-black">Cohort size</th><th class="px-4 py-3 font-black">D1 retention</th><th class="px-4 py-3 font-black">D7 retention</th><th class="px-4 py-3 font-black">D30 retention</th></tr></thead>
          <tbody>
            {#each dashboard.retention_cohorts as cohort (cohort.cohort_date)}
              <tr class="border-t border-line align-top"><th class="px-4 py-3 font-extrabold" scope="row">{formatUtcDate(cohort.cohort_date)}</th><td class="px-4 py-3 tabular-nums">{formatAnalyticsNumber(cohort.cohort_size)}</td><td class="px-4 py-3 tabular-nums">{retentionLabel(cohort.d1)}</td><td class="px-4 py-3 tabular-nums">{retentionLabel(cohort.d7)}</td><td class="px-4 py-3 tabular-nums">{retentionLabel(cohort.d30)}</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <EmptyState title="No account cohorts yet" message="Cohorts will appear after accounts and later activity have been recorded." />
    {/if}
  </Card>
{:else if !loadError}
  <EmptyState title="Audience analytics are warming up" message="Account growth and retention will appear as lifecycle events are recorded." />
{/if}
