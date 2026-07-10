<script lang="ts">
  import { Card, Notice } from '$lib/ui';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  const cards = $derived([
    {
      href: '/admin/moderation',
      label: 'Open reports',
      count: data.overview.open_report_count,
      detail: 'Review reports waiting for a decision.'
    },
    {
      href: '/admin/sources',
      label: 'Sources need attention',
      count: data.overview.source_attention_count,
      detail: `${data.overview.orphaned_source_count} need an account · ${data.overview.stale_source_count} stale · ${data.overview.pending_suggestion_count} pending suggestions`,
      secondary: `${data.overview.waiting_source_count} waiting · ${data.overview.healthy_source_count} healthy`
    },
    {
      href: '/admin/telegram',
      label: 'Telegram accounts need attention',
      count: data.overview.telegram_account_attention_count,
      detail: 'Reconnect, enable, or finish setting up these accounts.',
      secondary: `${data.overview.ready_telegram_account_count} ready`
    },
    {
      href: '/admin/content/seo',
      label: 'Missing SEO',
      count: data.overview.missing_seo_count,
      detail: 'Public, safe memes that need search details.'
    },
    {
      href: '/admin/content/templates',
      label: 'Templates to curate',
      count: data.overview.uncurated_template_count,
      detail: 'Templates that still need a curator review.'
    }
  ]);
</script>

<section class="grid gap-3">
  <p class="m-0 text-sm font-black uppercase tracking-[0.16em] text-muted">Admin overview</p>
  <h1 class="m-0 text-[clamp(2.4rem,8vw,5rem)] font-black leading-[0.9] tracking-[-0.075em]">What needs attention?</h1>
  <p class="m-0 max-w-2xl text-muted">Start with the work that needs a decision, then use the workspace for the details.</p>
</section>

{#if data.loadError}
  <Notice role="alert" tone="danger">{data.loadError}</Notice>
{/if}

<div class="mt-6 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
  {#each cards as card (card.href)}
    <Card class="m-0 p-0 transition hover:-translate-y-0.5 hover:shadow-[0_20px_45px_rgb(64_46_26_/_12%)]">
      <a href={card.href} class="grid h-full gap-4 rounded-[inherit] p-5 text-ink no-underline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ink">
        <div class="flex items-start justify-between gap-4">
          <h2 class="m-0 text-lg font-black tracking-[-0.03em]">{card.label}</h2>
          <span class="text-4xl font-black leading-none tracking-[-0.06em]">{card.count}</span>
        </div>
        <p class="m-0 text-sm text-muted">{card.detail}</p>
        {#if card.secondary}<p class="m-0 text-sm font-extrabold text-ink">{card.secondary}</p>{/if}
        <span class="text-sm font-extrabold underline decoration-2 underline-offset-4">Open workspace</span>
      </a>
    </Card>
  {/each}
</div>
