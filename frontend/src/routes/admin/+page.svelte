<script lang="ts">
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { ActionLink, Badge, Button, Input, Notice, Select } from '$lib/ui';
  import type { PageData } from './$types';
  import type { ActionData } from './$types';

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
  <div>
    <Badge>Signed in as {data.adminUser.email || data.adminUser.id}</Badge>
    <h1 class="my-3 text-[clamp(2.4rem,8vw,5.2rem)] font-black leading-[0.9] tracking-[-0.075em]">Admin tools</h1>
    <p class="m-0 text-muted">Initial browser-safe controls for source curation, templates, and moderation flags.</p>
  </div>
</section>

{#if form?.message}
  <Notice>{form.message}</Notice>
{/if}

{#if data.loadError}
  <Notice role="alert" tone="danger">{data.loadError}</Notice>
{/if}

<div class="my-5 grid gap-4 md:grid-cols-[minmax(0,1.2fr)_minmax(280px,0.8fr)]">
  <AdminPanel title="Channel Suggestions">
    {#if data.dashboard.suggestions.length === 0}
      <p class="m-0 text-muted">No suggestions yet.</p>
    {:else}
      {#each data.dashboard.suggestions as suggestion (suggestion.id)}
        <article class="grid items-center gap-3 border-t border-line pt-3 md:grid-cols-[minmax(0,1fr)_auto]">
          <div>
            <strong>{suggestion.channel_url}</strong>
            <p class="m-0 text-muted">{suggestion.platform} · {suggestion.status}</p>
          </div>
          <form method="POST" action="?/reviewSuggestion" class="flex flex-wrap items-center gap-2">
            <input type="hidden" name="suggestion_id" value={suggestion.id} />
            <Input name="admin_note" placeholder="note" value={suggestion.admin_note ?? ''} />
            <Button name="decision" value="approve" type="submit">Approve</Button>
            <Button name="decision" value="reject" type="submit" variant="secondary">Reject</Button>
          </form>
        </article>
      {/each}
    {/if}
  </AdminPanel>

  <AdminPanel title="Add Source Channel">
    <form method="POST" action="?/addSourceChannel" class="grid gap-3">
      <Select name="platform" aria-label="Platform">
        <option value="telegram">Telegram</option>
        <option value="reddit">Reddit</option>
        <option value="vk">VK</option>
      </Select>
      <Input name="platform_id" placeholder="platform id" required />
      <Input name="title" placeholder="title" required />
      <Input name="username" placeholder="username" />
      <Input name="session_id" placeholder="session" />
      <Input name="catchup_message_limit" type="number" min="1" max="10000" value="500" />
      <label class="inline-flex items-center gap-2 text-chiptext"><input name="catchup_enabled" type="checkbox" checked /> Catch-up enabled</label>
      <Button type="submit">Add channel</Button>
    </form>
  </AdminPanel>
</div>

<AdminPanel title="Source Channels">
  <div class="grid gap-3">
    {#each data.dashboard.sourceChannels as channel (channel.id)}
      <article class="grid items-center gap-3 border-t border-line pt-3 md:grid-cols-[minmax(0,1fr)_auto]">
        <div>
          <strong>{channel.title}</strong>
          <p class="m-0 text-muted">{channel.platform}:{channel.platform_id} · {channel.is_paused ? 'paused' : 'active'}</p>
        </div>
        <form method="POST" action="?/toggleSourceChannel">
          <input type="hidden" name="channel_id" value={channel.id} />
          <input type="hidden" name="paused" value={channel.is_paused ? 'false' : 'true'} />
          <Button type="submit">{channel.is_paused ? 'Resume' : 'Pause'}</Button>
        </form>
      </article>
    {/each}
  </div>
</AdminPanel>

<AdminPanel title="Meme Templates">
  <div class="grid gap-3">
    {#each data.dashboard.templates as template (template.id)}
      <form method="POST" action="?/updateTemplate" class="flex flex-wrap items-center gap-2">
        <input type="hidden" name="template_id" value={template.id} />
        <Input name="slug" value={template.slug} aria-label="Slug" />
        <Input name="name" value={template.name} aria-label="Name" />
        <Input name="description" value={template.description ?? ''} aria-label="Description" />
        <Input name="base_image_url" value={template.base_image_url ?? ''} aria-label="Base image URL" />
        <label class="inline-flex items-center gap-2 text-chiptext"><input name="is_curated" type="checkbox" checked={template.is_curated} /> Curated</label>
        <Button type="submit">Save</Button>
      </form>
    {/each}
  </div>
</AdminPanel>

<AdminPanel title="Moderation Reports Queue">
  <p class="m-0 text-muted">Open user/admin reports awaiting a decision. Resolving a report writes immutable decision history.</p>
  {#if data.dashboard.reports.length === 0}
    <p class="m-0 text-muted">No open moderation reports.</p>
  {:else}
    <div class="grid gap-3">
      {#each data.dashboard.reports as report (report.id)}
        <article class="grid items-start gap-3 border-t border-line pt-3 md:grid-cols-[minmax(0,1fr)_auto]">
          <div>
            <strong>{report.reason.toUpperCase()} report for {report.meme_id}</strong>
            <p class="m-0 text-muted">
              {report.status} · public {report.meme.is_public ? 'yes' : 'no'} · nsfw {report.meme.is_nsfw ? 'yes' : 'no'} · {report.created_at}
            </p>
            {#if report.note}
              <p>{report.note}</p>
            {/if}
          </div>
          <form method="POST" action="?/resolveModerationReport" class="flex flex-wrap items-center gap-2">
            <input type="hidden" name="report_id" value={report.id} />
            <Select name="action" aria-label="Resolution action">
              {#each reportActions as [value, label]}
                <option value={value}>{label}</option>
              {/each}
            </Select>
            <Select name="reason" aria-label="Decision reason">
              {#each moderationReasons as reason}
                <option value={reason} selected={reason === report.reason}>{reason}</option>
              {/each}
            </Select>
            <Input name="note" placeholder="decision note" />
            <Button type="submit">Resolve</Button>
          </form>
        </article>
      {/each}
    </div>
  {/if}
</AdminPanel>

<AdminPanel title="Moderation Decision History">
  {#if data.dashboard.decisions.length === 0}
    <p class="m-0 text-muted">No moderation decisions recorded yet.</p>
  {:else}
    <div class="grid gap-3">
      {#each data.dashboard.decisions as decision (decision.id)}
        <article class="grid gap-2 border-t border-line pt-3">
          <div>
            <strong>{decision.action} · {decision.meme_id}</strong>
            <p class="m-0 text-muted">
              public {decision.previous_is_public ? 'yes' : 'no'} -> {decision.new_is_public ? 'yes' : 'no'} · nsfw {decision.previous_is_nsfw ? 'yes' : 'no'} -> {decision.new_is_nsfw ? 'yes' : 'no'}
            </p>
            <p class="m-0 text-muted">{decision.reason ?? 'no reason'} · {decision.created_at}</p>
            {#if decision.note}
              <p>{decision.note}</p>
            {/if}
          </div>
        </article>
      {/each}
    </div>
  {/if}
</AdminPanel>

<AdminPanel title="Meme Moderation">
  <p class="m-0 text-muted">Direct public and NSFW flag overrides are preserved for admin emergencies. Every submission writes a moderation decision audit record.</p>
  <div class="grid gap-3">
    {#each data.dashboard.memes as meme (meme.id)}
      <form method="POST" action="?/updateMemeModeration" class="grid items-center gap-3 border-t border-line pt-3 md:grid-cols-[minmax(0,1fr)_auto_auto_auto_auto_auto_auto]">
        <input type="hidden" name="meme_id" value={meme.id} />
        <div>
          <strong>{meme.id}</strong>
          <p class="m-0 text-muted">{meme.media_type} · {meme.language} · score {meme.popularity_score.toFixed(1)}</p>
        </div>
        <ActionLink size="compact" variant="secondary" href={`/admin/memes/${meme.id}`}>Open detail</ActionLink>
        <label class="inline-flex items-center gap-2 text-chiptext"><input name="is_public" type="checkbox" checked={meme.is_public} /> Public</label>
        <label class="inline-flex items-center gap-2 text-chiptext"><input name="is_nsfw" type="checkbox" checked={meme.is_nsfw} /> NSFW</label>
        <Select name="reason" aria-label="Override reason">
          <option value="">No reason</option>
          {#each moderationReasons as reason}
            <option value={reason}>{reason}</option>
          {/each}
        </Select>
        <Input name="note" placeholder="audit note" />
        <Button type="submit">Update audited flags</Button>
      </form>
    {/each}
  </div>
</AdminPanel>
