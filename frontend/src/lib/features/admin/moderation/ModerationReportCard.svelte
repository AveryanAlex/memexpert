<script lang="ts">
  import type { AdminModerationReportRead } from '$lib/api/types';
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import { Badge, Button, Card, FormRow, Input, Select } from '$lib/ui';
  import AdminMediaPreview from './AdminMediaPreview.svelte';

  let { report }: { report: AdminModerationReportRead } = $props();

  const actions = [
    ['no_action', 'Dismiss — no action'],
    ['mark_nsfw', 'Mark as sensitive'],
    ['mark_sfw', 'Mark as safe'],
    ['hide', 'Hide from catalog'],
    ['hide_and_mark_nsfw', 'Hide and mark sensitive'],
    ['publish', 'Publish in catalog']
  ] as const;

  const isOpen = $derived(report.status === 'pending' || report.status === 'in_review');

  function plain(value: string): string {
    return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase());
  }
</script>

<Card class="m-0 grid gap-4 p-4 lg:grid-cols-[minmax(220px,0.7fr)_minmax(0,1.3fr)]">
  <AdminMediaPreview meme={report.meme} label={`Preview for ${plain(report.reason)} report`} compact />

  <div class="grid content-start gap-4">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">{plain(report.reason)} report</p>
        <h2 class="mb-0 mt-1 text-2xl font-black tracking-[-0.04em]">Needs a moderation decision</h2>
      </div>
      <Badge>{plain(report.status)}</Badge>
    </div>

    <dl class="m-0 grid gap-2 text-sm sm:grid-cols-2">
      <div><dt class="font-extrabold text-muted">Reported</dt><dd class="m-0"><time datetime={report.created_at}>{formatAdminTimestamp(report.created_at)}</time></dd></div>
      <div><dt class="font-extrabold text-muted">Current state</dt><dd class="m-0">{report.meme.is_public ? 'Visible' : 'Hidden'} · {report.meme.is_nsfw ? 'Sensitive' : 'Safe'}</dd></div>
    </dl>

    {#if report.note}
      <div class="rounded-xl border border-line bg-soft/50 p-3"><strong>Reporter note</strong><p class="mb-0 mt-1">{report.note}</p></div>
    {/if}

    <a class="w-fit text-sm font-black underline decoration-2 underline-offset-4" href={`/admin/memes/${report.meme_id}`}>Open full meme detail</a>

    {#if isOpen}
      <form method="POST" action="?/resolveModerationReport" class="grid gap-3 rounded-2xl border border-line bg-cream/50 p-4">
        <input type="hidden" name="report_id" value={report.id} />
        <input type="hidden" name="reason" value={report.reason} />
        <FormRow label="Resolution">
          <Select name="action">
            {#each actions as [value, label]}
              <option value={value}>{label}</option>
            {/each}
          </Select>
        </FormRow>
        <FormRow label="Decision note (optional)">
          <Input name="note" placeholder="Add context for the audit history" />
        </FormRow>
        <Button type="submit">Record decision</Button>
      </form>
    {:else}
      <p class="m-0 text-sm text-muted">This report is closed.</p>
    {/if}

    <AdvancedSection title="Meme metadata" description="Technical context for exceptional review.">
      <dl class="m-0 grid gap-2 text-sm sm:grid-cols-2">
        <div><dt class="font-extrabold text-muted">Media type</dt><dd class="m-0">{plain(report.meme.media_type)}</dd></div>
        <div><dt class="font-extrabold text-muted">Language</dt><dd class="m-0">{report.meme.language}</dd></div>
        <div><dt class="font-extrabold text-muted">Tags</dt><dd class="m-0">{report.meme.tags.join(', ') || 'None'}</dd></div>
        <div><dt class="font-extrabold text-muted">Meme ID</dt><dd class="m-0 [overflow-wrap:anywhere]">{report.meme.id}</dd></div>
      </dl>
    </AdvancedSection>
  </div>
</Card>
