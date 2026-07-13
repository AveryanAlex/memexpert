<script lang="ts">
  import type { AdminMemeRead, AdminModerationDecisionRead, AdminModerationReportRead } from '$lib/api/types';
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import { Badge, Button, Card, EmptyState, Input, Notice, Select } from '$lib/ui';
  import AdminMediaPreview from './AdminMediaPreview.svelte';
  import ModerationReportCard from './ModerationReportCard.svelte';

  let {
    moderation,
    loadError,
    form
  }: {
    moderation: { reports: AdminModerationReportRead[]; decisions: AdminModerationDecisionRead[]; memes: AdminMemeRead[] };
    loadError: string | null;
    form: { message?: string; error?: boolean } | null;
  } = $props();

  const reasons = ['nsfw', 'spam', 'harassment', 'copyright', 'illegal', 'other'];

  function plain(value: string): string {
    return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase());
  }
</script>

<section class="grid gap-3">
  <p class="m-0 text-sm font-black uppercase tracking-[0.16em] text-muted">Moderation</p>
  <h1 class="m-0 text-[clamp(2.4rem,8vw,5rem)] font-black leading-[0.9] tracking-[-0.075em]">Reports needing a decision</h1>
  <div class="flex flex-wrap items-center justify-between gap-3">
    <p class="m-0 max-w-2xl text-muted">Review the reported media and current state before recording a clear outcome.</p>
    <a class="text-sm font-black underline decoration-2 underline-offset-4" href="/admin/moderation/patterns">Manage blocked media patterns</a>
  </div>
</section>

{#if form?.message}
  <Notice role={form.error ? 'alert' : undefined} tone={form.error ? 'danger' : 'success'}>{form.message}</Notice>
{/if}
{#if loadError}<Notice role="alert" tone="danger">{loadError}</Notice>{/if}

<section class="mt-6 grid gap-4" aria-labelledby="report-queue-heading">
  <div class="flex items-end justify-between gap-3">
    <div><p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">Primary queue</p><h2 id="report-queue-heading" class="m-0 text-3xl font-black tracking-[-0.05em]">Open reports</h2></div>
    <Badge>{moderation.reports.length} open</Badge>
  </div>
  {#if moderation.reports.length}
    {#each moderation.reports as report (report.id)}<ModerationReportCard {report} />{/each}
  {:else}
    <EmptyState title="No reports need attention" message="New user reports will appear here." />
  {/if}
</section>

<div class="mt-5 grid gap-4">
  <AdvancedSection title="Review a meme without a report" description="Use direct overrides only when an operator found an issue outside the report queue.">
    <div class="grid gap-4">
      {#each moderation.memes as meme (meme.id)}
        <Card class="m-0 grid gap-4 p-4 lg:grid-cols-[180px_minmax(0,1fr)]">
          <AdminMediaPreview {meme} compact label="Direct review preview" />
          <div class="grid gap-3">
            <div class="flex flex-wrap justify-between gap-2"><strong>{meme.is_public ? 'Visible' : 'Hidden'} · {meme.is_nsfw ? 'Sensitive' : 'Safe'}</strong><a class="text-sm font-black underline" href={`/admin/memes/${meme.id}`}>Open detail</a></div>
            <form method="POST" action="?/updateMemeModeration" class="grid gap-3 sm:grid-cols-2">
              <input type="hidden" name="meme_id" value={meme.id} />
              <label class="grid gap-2 text-sm font-extrabold">Visibility policy<Select name="visibility_mode"><option value="auto" selected={meme.visibility_mode === 'auto'}>Automatic from provenance</option><option value="force_public" selected={meme.visibility_mode === 'force_public'}>Force public</option><option value="force_private" selected={meme.visibility_mode === 'force_private'}>Force private</option></Select><span class="font-normal text-muted">Effective state: {meme.is_public ? 'visible' : 'hidden'}</span></label>
              <label class="inline-flex items-center gap-2 text-sm font-extrabold"><input name="is_nsfw" type="checkbox" checked={meme.is_nsfw} /> Sensitive content</label>
              <label class="grid gap-2 text-sm font-extrabold">Reason<Select name="reason"><option value="">No reason</option>{#each reasons as reason}<option value={reason}>{plain(reason)}</option>{/each}</Select></label>
              <label class="grid gap-2 text-sm font-extrabold">Audit note (optional)<Input name="note" placeholder="Why is this override needed?" /></label>
              <Button type="submit" class="sm:col-span-2">Save direct override</Button>
            </form>
          </div>
        </Card>
      {:else}
        <p class="m-0 text-muted">No recent memes are available for direct review.</p>
      {/each}
    </div>
  </AdvancedSection>

  <AdvancedSection title="Recent decisions" description="Latest moderation outcomes and their audit notes.">
    {#if moderation.decisions.length}
      <div class="grid gap-3">
        {#each moderation.decisions as decision (decision.id)}
          <article class="grid gap-1 border-t border-line pt-3 text-sm">
            <strong>{plain(decision.action)}</strong>
            <span class="text-muted"><time datetime={decision.created_at}>{formatAdminTimestamp(decision.created_at)}</time> · {decision.new_is_public ? 'Visible' : 'Hidden'} · {decision.new_is_nsfw ? 'Sensitive' : 'Safe'}</span>
            {#if decision.note}<p class="m-0">{decision.note}</p>{/if}
            <a class="w-fit font-black underline" href={`/admin/memes/${decision.meme_id}`}>Open meme detail</a>
          </article>
        {/each}
      </div>
    {:else}<p class="m-0 text-muted">No moderation decisions recorded yet.</p>{/if}
  </AdvancedSection>
</div>
