<script lang="ts">
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import AdminMediaPreview from '$lib/features/admin/moderation/AdminMediaPreview.svelte';
  import { Badge, Button, Card, FormRow, Input, Label, Notice, Select } from '$lib/ui';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

  const moderationReasons = ['nsfw', 'spam', 'harassment', 'copyright', 'illegal', 'other'];
  const reportActions = [
    ['no_action', 'Dismiss — no action'],
    ['mark_nsfw', 'Mark as sensitive'],
    ['mark_sfw', 'Mark as safe'],
    ['hide', 'Hide from catalog'],
    ['hide_and_mark_nsfw', 'Hide and mark sensitive'],
    ['publish', 'Publish in catalog']
  ];
  const openReports = $derived(data.detail?.reports.filter((report) => report.status === 'pending' || report.status === 'in_review') ?? []);
  const closedReports = $derived(data.detail?.reports.filter((report) => report.status !== 'pending' && report.status !== 'in_review') ?? []);
  const formError = $derived(Boolean(form && 'error' in form && form.error));

  function plain(value: string): string {
    return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase());
  }
</script>

<section class="grid gap-3">
  <p class="m-0"><a class="text-sm font-black underline decoration-2 underline-offset-4" href="/admin/moderation">Back to moderation</a></p>
  <h1 class="m-0 text-[clamp(2.4rem,8vw,5.2rem)] font-black leading-[0.9] tracking-[-0.075em]">Review meme</h1>
  <p class="m-0 text-muted">Start with the media, current state, and open reports. Use overrides only when a change is needed.</p>
</section>

{#if form?.message}<Notice role={formError ? 'alert' : 'status'} tone={formError ? 'danger' : 'success'}>{form.message}</Notice>{/if}
{#if data.loadError}<Notice role="alert" tone="danger">{data.loadError}</Notice>{/if}

{#if data.detail}
  {@const detail = data.detail}
  <div class="my-5 grid items-start gap-4 lg:grid-cols-[minmax(0,1.2fr)_minmax(280px,0.8fr)]">
    <Card class="m-0 grid gap-3">
      <div class="flex flex-wrap items-center justify-between gap-3"><h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Preview</h2><Badge>{plain(detail.meme.media_type)}</Badge></div>
      <AdminMediaPreview meme={detail.meme} label="Admin meme preview" />
    </Card>

    <Card class="m-0 grid gap-4">
      <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Current state</h2>
      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-1">
        <div class="rounded-2xl border border-line bg-soft/50 p-4"><span class="text-sm font-extrabold text-muted">Catalog visibility</span><p class="mb-0 mt-1 text-2xl font-black">{detail.meme.is_public ? 'Visible' : 'Hidden'}</p></div>
        <div class="rounded-2xl border border-line bg-soft/50 p-4"><span class="text-sm font-extrabold text-muted">Visibility policy</span><p class="mb-0 mt-1 text-2xl font-black">{plain(detail.meme.visibility_mode)}</p></div>
        <div class="rounded-2xl border border-line bg-soft/50 p-4"><span class="text-sm font-extrabold text-muted">Safety label</span><p class="mb-0 mt-1 text-2xl font-black">{detail.meme.is_nsfw ? 'Sensitive' : 'Safe'}</p></div>
      </div>
      <p class="m-0 text-sm text-muted">{openReports.length} open {openReports.length === 1 ? 'report' : 'reports'} · {detail.decisions.length} recorded {detail.decisions.length === 1 ? 'decision' : 'decisions'}</p>
    </Card>
  </div>

  <Card class="my-4 grid gap-4">
    <div><p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">Needs attention</p><h2 class="m-0 text-3xl font-black tracking-[-0.05em]">Open reports</h2></div>
    {#if openReports.length === 0}
      <p class="m-0 text-muted">No open reports for this meme.</p>
    {:else}
      {#each openReports as report (report.id)}
        <article class="grid gap-3 border-t border-line pt-4">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div><strong>{plain(report.reason)} report</strong><p class="m-0 text-sm text-muted"><time datetime={report.created_at}>{formatAdminTimestamp(report.created_at)}</time> · {plain(report.status)}</p></div>
            <Badge>{detail.meme.is_public ? 'Visible' : 'Hidden'} · {detail.meme.is_nsfw ? 'Sensitive' : 'Safe'}</Badge>
          </div>
          {#if report.note}<p class="m-0 rounded-xl border border-line bg-soft/50 p-3">{report.note}</p>{/if}
          <form method="POST" action="?/resolveReport" class="grid gap-3 md:grid-cols-[minmax(180px,0.8fr)_minmax(180px,0.8fr)_minmax(220px,1fr)_auto]">
            <input type="hidden" name="report_id" value={report.id} />
            <Select name="action" aria-label="Resolution">
              {#each reportActions as [value, label]}<option value={value}>{label}</option>{/each}
            </Select>
            <Select name="reason" aria-label="Decision reason">
              {#each moderationReasons as reason}<option value={reason} selected={reason === report.reason}>{plain(reason)}</option>{/each}
            </Select>
            <Input name="note" aria-label="Decision note" placeholder="Decision note (optional)" />
            <Button type="submit">Record decision</Button>
          </form>
        </article>
      {/each}
    {/if}
  </Card>

  <Card class="my-4 grid gap-4">
    <div><p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">Operator controls</p><h2 class="m-0 text-3xl font-black tracking-[-0.05em]">Overrides</h2><p class="mb-0 mt-1 text-sm text-muted">Changes are recorded in moderation history.</p></div>
    <form method="POST" action="?/updateMeme" class="grid gap-4 md:grid-cols-2">
      <FormRow label="Visibility policy" hint={`Effective state: ${detail.meme.is_public ? 'visible' : 'hidden'}`}><Select name="visibility_mode"><option value="auto" selected={detail.meme.visibility_mode === 'auto'}>Automatic from provenance</option><option value="force_public" selected={detail.meme.visibility_mode === 'force_public'}>Force public</option><option value="force_private" selected={detail.meme.visibility_mode === 'force_private'}>Force private</option></Select></FormRow>
      <Label class="!inline-flex items-center gap-2"><input name="is_nsfw" type="checkbox" checked={detail.meme.is_nsfw} /> Sensitive content</Label>
      <FormRow label="Template"><Select name="template_id"><option value="" selected={detail.meme.template_id === null}>No template</option>{#each data.templates as template (template.id)}<option value={template.id} selected={template.id === detail.meme.template_id}>{template.name}</option>{/each}</Select></FormRow>
      <FormRow label="Reason"><Select name="reason"><option value="">No reason</option>{#each moderationReasons as reason}<option value={reason}>{plain(reason)}</option>{/each}</Select></FormRow>
      <FormRow label="Audit note (optional)" class="md:col-span-2"><Input name="note" placeholder="Why is this override needed?" /></FormRow>
      <Button type="submit" class="md:col-span-2">Save overrides</Button>
    </form>
  </Card>

  <div class="my-4 grid gap-4">
    <AdvancedSection title="Metadata" description="Technical and catalog details for exceptional review.">
      <dl class="m-0 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <div><dt class="text-xs font-extrabold uppercase text-muted">Meme ID</dt><dd class="m-0 [overflow-wrap:anywhere]">{detail.meme.id}</dd></div>
        <div><dt class="text-xs font-extrabold uppercase text-muted">Language</dt><dd class="m-0">{detail.meme.language}</dd></div>
        <div><dt class="text-xs font-extrabold uppercase text-muted">Popularity</dt><dd class="m-0">{detail.meme.popularity_score.toFixed(1)}</dd></div>
        <div><dt class="text-xs font-extrabold uppercase text-muted">Likes</dt><dd class="m-0">{detail.meme.like_count}</dd></div>
        <div><dt class="text-xs font-extrabold uppercase text-muted">Tags</dt><dd class="m-0">{detail.meme.tags.join(', ') || 'None'}</dd></div>
        <div><dt class="text-xs font-extrabold uppercase text-muted">Created</dt><dd class="m-0"><time datetime={detail.meme.created_at}>{formatAdminTimestamp(detail.meme.created_at)}</time></dd></div>
        <div><dt class="text-xs font-extrabold uppercase text-muted">Visibility policy</dt><dd class="m-0">{plain(detail.meme.visibility_mode)}</dd></div>
      </dl>
    </AdvancedSection>

    <AdvancedSection title="Moderation history" description="Closed reports and immutable admin decisions.">
      <div class="grid gap-5">
        <section><h3 class="mt-0">Recorded decisions</h3>{#if detail.decisions.length}{#each detail.decisions as decision (decision.id)}<article class="border-t border-line py-3"><strong>{plain(decision.action)}</strong><p class="m-0 text-sm text-muted"><time datetime={decision.created_at}>{formatAdminTimestamp(decision.created_at)}</time> · {plain(decision.new_visibility_mode)} → {decision.new_is_public ? 'Visible' : 'Hidden'} · {decision.new_is_nsfw ? 'Sensitive' : 'Safe'}</p>{#if decision.note}<p class="mb-0">{decision.note}</p>{/if}</article>{/each}{:else}<p class="m-0 text-muted">No admin decisions recorded yet.</p>{/if}</section>
        <section><h3 class="mt-0">Closed reports</h3>{#if closedReports.length}{#each closedReports as report (report.id)}<p class="border-t border-line py-3"><strong>{plain(report.reason)}</strong> · {plain(report.status)} {report.resolved_at ? `on ${formatAdminTimestamp(report.resolved_at)}` : ''}</p>{/each}{:else}<p class="m-0 text-muted">No closed reports.</p>{/if}</section>
      </div>
    </AdvancedSection>

    <AdvancedSection title="Danger zone" description="Merge or permanently delete this meme. Both actions create a durable audit snapshot first." danger>
      <div class="grid gap-5 lg:grid-cols-2">
        <form method="POST" action="?/mergeMeme" class="grid gap-3 rounded-2xl border border-danger-line bg-paper/70 p-4">
          <h3 class="m-0 text-xl font-black">Merge into another meme</h3>
          <p class="m-0 text-sm text-muted">Transfers files, saves, pins, and popularity lineage into the target, then permanently removes this source meme. This cannot be undone.</p>
          <FormRow label="Target meme ID"><Input name="target_meme_id" placeholder="Target UUID" required /></FormRow>
          <FormRow label="Required audit note"><Input name="note" placeholder="Why should these memes be merged?" required /></FormRow>
          <FormRow label="Type MERGE to confirm"><Input name="confirmation_phrase" autocomplete="off" spellcheck="false" required /></FormRow>
          <Button type="submit" variant="danger">Merge meme</Button>
        </form>

        <form method="POST" action="?/deleteMeme" class="grid gap-3 rounded-2xl border border-danger-line bg-paper/70 p-4">
          <h3 class="m-0 text-xl font-black">Delete permanently</h3>
          <p class="m-0 text-sm text-muted">Permanently removes the meme, media references, reports, saves, and other dependent records after an audit snapshot. This cannot be undone.</p>
          <FormRow label="Required audit note"><Input name="note" placeholder="Why is deletion necessary?" required /></FormRow>
          <FormRow label="Type DELETE to confirm"><Input name="confirmation_phrase" autocomplete="off" spellcheck="false" required /></FormRow>
          <Button type="submit" variant="danger">Delete permanently</Button>
        </form>
      </div>
    </AdvancedSection>
  </div>
{/if}
