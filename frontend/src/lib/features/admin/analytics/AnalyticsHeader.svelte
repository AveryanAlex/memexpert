<script lang="ts">
  import { Button, FormRow, Input, PageHeader } from '$lib/ui';
  import {
    ADMIN_ANALYTICS_PRESETS,
    adminAnalyticsHref,
    adminAnalyticsPresetHref,
    analyticsRangeForControls,
    analyticsRangeLabel,
    dateDurationDays,
    type AdminAnalyticsRangeParams,
    type AdminAnalyticsResolvedRange
  } from './range';

  export type AdminAnalyticsSection = 'overview' | 'engagement' | 'audience' | 'content';

  let {
    activeSection,
    currentPath,
    title,
    description,
    range,
    requestedRange
  }: {
    activeSection: AdminAnalyticsSection;
    currentPath: string;
    title: string;
    description: string;
    range: AdminAnalyticsResolvedRange | null;
    requestedRange: AdminAnalyticsRangeParams;
  } = $props();

  const controls = $derived(analyticsRangeForControls(range, requestedRange));
  const selectedDays = $derived(dateDurationDays(range));
  const sections: Array<{ key: AdminAnalyticsSection; label: string; href: string }> = $derived([
    { key: 'overview', label: 'Overview', href: adminAnalyticsHref('/admin/analytics', range ?? requestedRange) },
    { key: 'engagement', label: 'Engagement', href: adminAnalyticsHref('/admin/analytics/engagement', range ?? requestedRange) },
    { key: 'audience', label: 'Audience', href: adminAnalyticsHref('/admin/analytics/audience', range ?? requestedRange) },
    { key: 'content', label: 'Content & sources', href: adminAnalyticsHref('/admin/analytics/content', range ?? requestedRange) }
  ]);
</script>

<PageHeader eyebrow="Admin analytics" {title} {description}>
  {#if range}
    <span class="rounded-full border border-line bg-paper px-3 py-2 text-sm font-extrabold text-ink">{analyticsRangeLabel(range)}</span>
  {/if}
</PageHeader>

<nav class="mb-5 overflow-x-auto border-b border-line" aria-label="Analytics sections">
  <div class="flex min-w-max gap-1 pb-px">
    {#each sections as section (section.key)}
      {@const active = section.key === activeSection}
      <a
        href={section.href}
        aria-current={active ? 'page' : undefined}
        class={active
          ? 'rounded-t-2xl border border-b-paper border-line bg-paper px-4 py-3 text-sm font-black text-ink no-underline'
          : 'rounded-t-2xl px-4 py-3 text-sm font-extrabold text-muted no-underline hover:bg-soft hover:text-ink'}
      >{section.label}</a>
    {/each}
  </div>
</nav>

<section class="mb-7 grid gap-4 rounded-3xl border border-line bg-soft p-4 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-end" aria-label="Analytics date range">
  <div class="grid gap-2">
    <p class="m-0 text-xs font-black uppercase tracking-[0.16em] text-muted">Reporting window</p>
    <div class="flex flex-wrap gap-2" aria-label="Quick date ranges">
      {#each ADMIN_ANALYTICS_PRESETS as preset (preset.days)}
        {@const selected = selectedDays === preset.days}
        <a
          href={adminAnalyticsPresetHref(currentPath, preset.days)}
          aria-current={selected ? 'true' : undefined}
          class={selected
            ? 'rounded-full bg-ink px-3 py-2 text-sm font-extrabold text-paper no-underline'
            : 'rounded-full border border-line bg-paper px-3 py-2 text-sm font-extrabold text-ink no-underline hover:bg-cream'}
        >{preset.label}</a>
      {/each}
    </div>
  </div>

  <form method="GET" action={currentPath} class="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end">
    <FormRow label="Start date">
      <Input type="date" name="start_date" value={controls.startDate ?? ''} required />
    </FormRow>
    <FormRow label="End date">
      <Input type="date" name="end_date" value={controls.endDate ?? ''} required />
    </FormRow>
    <Button type="submit" variant="secondary">Apply</Button>
  </form>
</section>
