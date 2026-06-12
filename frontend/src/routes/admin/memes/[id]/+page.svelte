<script lang="ts">
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { ActionLink, Badge, Button, Input, Notice, Select } from '$lib/ui';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

  const moderationReasons = ['nsfw', 'spam', 'harassment', 'copyright', 'illegal', 'other'];
  const reportActions = [
    ['mark_nsfw', 'Mark NSFW'],
    ['mark_sfw', 'Mark SFW'],
    ['hide', 'Hide'],
    ['hide_and_mark_nsfw', 'Hide + NSFW'],
    ['publish', 'Publish'],
    ['no_action', 'No action']
  ];
</script>

<section>
  <p><a class="text-sm font-black underline decoration-2 underline-offset-4" href="/admin">Back to admin list</a></p>
  <Badge>Signed in as {data.adminUser.email || data.adminUser.id}</Badge>
  <h1 class="my-3 text-[clamp(2.4rem,8vw,5.2rem)] font-black leading-[0.9] tracking-[-0.075em]">Meme detail</h1>
  <p class="m-0 text-muted">Review reports, override visibility/classification, and inspect audit history.</p>
</section>

{#if form?.message}
  <Notice>{form.message}</Notice>
{/if}

{#if data.loadError}
  <Notice role="alert" tone="danger">{data.loadError}</Notice>
{/if}

{#if data.detail}
  {@const detail = data.detail}
  <div class="my-5 grid items-start gap-4 md:grid-cols-[minmax(0,1.2fr)_minmax(280px,0.8fr)]">
    <AdminPanel title="Metadata">
      <dl class="m-0 grid gap-2">
        <div class="grid gap-1 border-t border-line pt-2"><dt class="text-xs font-extrabold uppercase text-muted">ID</dt><dd class="m-0 [overflow-wrap:anywhere]">{detail.meme.id}</dd></div>
        <div class="grid gap-1 border-t border-line pt-2"><dt class="text-xs font-extrabold uppercase text-muted">Media</dt><dd class="m-0">{detail.meme.media_type}</dd></div>
        <div class="grid gap-1 border-t border-line pt-2"><dt class="text-xs font-extrabold uppercase text-muted">Language</dt><dd class="m-0">{detail.meme.language}</dd></div>
        <div class="grid gap-1 border-t border-line pt-2"><dt class="text-xs font-extrabold uppercase text-muted">Score</dt><dd class="m-0">{detail.meme.popularity_score.toFixed(1)}</dd></div>
        <div class="grid gap-1 border-t border-line pt-2"><dt class="text-xs font-extrabold uppercase text-muted">Likes</dt><dd class="m-0">{detail.meme.like_count}</dd></div>
        <div class="grid gap-1 border-t border-line pt-2"><dt class="text-xs font-extrabold uppercase text-muted">Author</dt><dd class="m-0 [overflow-wrap:anywhere]">{detail.meme.author_user_id ?? 'none'}</dd></div>
        <div class="grid gap-1 border-t border-line pt-2"><dt class="text-xs font-extrabold uppercase text-muted">Tags</dt><dd class="m-0">{detail.meme.tags.length ? detail.meme.tags.join(', ') : 'none'}</dd></div>
        <div class="grid gap-1 border-t border-line pt-2"><dt class="text-xs font-extrabold uppercase text-muted">Created</dt><dd class="m-0">{detail.meme.created_at}</dd></div>
      </dl>
    </AdminPanel>

    <AdminPanel title="Overrides">
      <form method="POST" action="?/updateMeme" class="grid gap-3">
        <label class="inline-flex items-center gap-2 text-chiptext"><input name="is_public" type="checkbox" checked={detail.meme.is_public} /> Public</label>
        <label class="inline-flex items-center gap-2 text-chiptext"><input name="is_nsfw" type="checkbox" checked={detail.meme.is_nsfw} /> NSFW</label>
        <label class="grid gap-2 text-chiptext">
          Template
          <Select name="template_id">
            <option value="" selected={detail.meme.template_id === null}>No template</option>
            {#each data.templates as template (template.id)}
              <option value={template.id} selected={template.id === detail.meme.template_id}>{template.name}</option>
            {/each}
          </Select>
        </label>
        <label class="grid gap-2 text-chiptext">
          Reason
          <Select name="reason">
            <option value="">No reason</option>
            {#each moderationReasons as reason}
              <option value={reason}>{reason}</option>
            {/each}
          </Select>
        </label>
        <Input name="note" placeholder="audit note" />
        <Button type="submit">Save overrides</Button>
      </form>

      <div class="mt-4 grid gap-3 border-t border-line pt-4" aria-label="Destructive actions">
        <h3 class="m-0 text-xl font-black">Destructive actions</h3>
        <p class="m-0 font-extrabold text-danger">Irreversible. These actions record a durable admin audit before the source meme row is removed.</p>

        <form method="POST" action="?/deleteMeme" class="grid gap-3 rounded-[18px] border border-danger-line bg-danger-surface p-4">
          <strong>Delete meme</strong>
          <p class="m-0 text-muted">Removes the meme and cascaded dependents after snapshotting files, SEO, saves, pins, reports, decisions, popularity, and sync state.</p>
          <label class="grid gap-2 text-chiptext">
            Type meme id to confirm
            <Input name="confirmation" placeholder={detail.meme.id} required />
          </label>
          <label class="grid gap-2 text-chiptext">
            Required audit note
            <Input name="note" placeholder="why this deletion is necessary" required />
          </label>
          <Button type="submit" variant="danger">Delete permanently</Button>
        </form>

        <form method="POST" action="?/mergeMeme" class="grid gap-3 rounded-[18px] border border-danger-line bg-danger-surface p-4">
          <strong>Merge into another meme</strong>
          <p class="m-0 text-muted">Transfers files, collection saves, pins, and popularity lineage into the target, then deletes this source meme.</p>
          <label class="grid gap-2 text-chiptext">
            Target meme id
            <Input name="target_meme_id" placeholder="target UUID" required />
          </label>
          <label class="grid gap-2 text-chiptext">
            Type source meme id to confirm
            <Input name="confirmation" placeholder={detail.meme.id} required />
          </label>
          <label class="grid gap-2 text-chiptext">
            Required audit note
            <Input name="note" placeholder="why these memes should be merged" required />
          </label>
          <Button type="submit" variant="danger">Merge into target</Button>
        </form>
      </div>
    </AdminPanel>
  </div>

  <AdminPanel title="Reports">
    {#if detail.reports.length === 0}
      <p class="m-0 text-muted">No reports for this meme.</p>
    {:else}
      <div class="grid gap-3">
        {#each detail.reports as report (report.id)}
          <article class="grid items-start gap-3 border-t border-line pt-3 md:grid-cols-[minmax(0,1fr)_auto]">
            <div>
              <strong>{report.reason.toUpperCase()} report</strong>
              <p class="m-0 text-muted">{report.status} - reporter {report.reporter_user_id ?? 'anonymous'} - {report.created_at}</p>
              {#if report.note}<p>{report.note}</p>{/if}
              {#if report.resolved_at}<p class="m-0 text-muted">Resolved {report.resolved_at} by {report.resolved_by_admin_user_id ?? 'unknown'}</p>{/if}
            </div>
            <form method="POST" action="?/resolveReport" class="flex flex-wrap items-center gap-2">
              <input type="hidden" name="report_id" value={report.id} />
              <Select name="action" aria-label="Resolution action" disabled={report.status !== 'pending' && report.status !== 'in_review'}>
                {#each reportActions as [value, label]}
                  <option value={value}>{label}</option>
                {/each}
              </Select>
              <Select name="reason" aria-label="Decision reason" disabled={report.status !== 'pending' && report.status !== 'in_review'}>
                {#each moderationReasons as reason}
                  <option value={reason} selected={reason === report.reason}>{reason}</option>
                {/each}
              </Select>
              <Input name="note" placeholder="decision note" disabled={report.status !== 'pending' && report.status !== 'in_review'} />
              <Button type="submit" disabled={report.status !== 'pending' && report.status !== 'in_review'}>Resolve</Button>
            </form>
          </article>
        {/each}
      </div>
    {/if}
  </AdminPanel>

  <AdminPanel title="Decision History">
    {#if detail.decisions.length === 0}
      <p class="m-0 text-muted">No admin changes recorded yet.</p>
    {:else}
      <div class="grid gap-3">
        {#each detail.decisions as decision (decision.id)}
          <article class="grid gap-2 border-t border-line pt-3">
            <div>
              <strong>{decision.action}</strong>
              <p class="m-0 text-muted">{decision.created_at} - admin {decision.admin_user_id ?? 'unknown'}</p>
            </div>
            <code class="whitespace-normal [overflow-wrap:anywhere]">
              public {decision.previous_is_public ? 'yes' : 'no'} -> {decision.new_is_public ? 'yes' : 'no'};
              nsfw {decision.previous_is_nsfw ? 'yes' : 'no'} -> {decision.new_is_nsfw ? 'yes' : 'no'};
              template {decision.previous_template_id ?? 'none'} -> {decision.new_template_id ?? 'none'}
            </code>
            {#if decision.note}<p>{decision.note}</p>{/if}
          </article>
        {/each}
      </div>
    {/if}
  </AdminPanel>
{/if}
