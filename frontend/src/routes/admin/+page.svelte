<script lang="ts">
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { ActionLink, Badge, Button, EmptyState, FormRow, Input, Notice, Select, Textarea } from '$lib/ui';
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

  function freshnessCopy(channel: PageData['dashboard']['sourceChannels'][number]): string {
    if (channel.freshness_status === 'fresh') {
      return `fresh${channel.seconds_since_last_fetch === null ? '' : ` · ${formatAge(channel.seconds_since_last_fetch)} ago`}`;
    }
    if (channel.freshness_status === 'stale') {
      return `stale · ${formatAge(channel.seconds_since_last_fetch ?? 0)} ago`;
    }
    if (channel.freshness_status === 'checkpoint_only') {
      return `checkpoint only · last post ${channel.last_read_post_id}`;
    }
    return 'never fetched';
  }

  function formatAge(seconds: number): string {
    if (seconds >= 86400) return `${Math.floor(seconds / 86400)}d`;
    if (seconds >= 3600) return `${Math.floor(seconds / 3600)}h`;
    if (seconds >= 60) return `${Math.floor(seconds / 60)}m`;
    return `${seconds}s`;
  }

  function templateTargets(templateId: string): PageData['dashboard']['templates'] {
    return data.dashboard.templates.filter((template) => template.id !== templateId);
  }
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
      <Input name="telegram_session_name" placeholder="Telegram session name" />
      <Input name="catchup_message_limit" type="number" min="1" max="10000" value="500" />
      <label class="inline-flex items-center gap-2 text-chiptext"><input name="catchup_enabled" type="checkbox" checked /> Catch-up enabled</label>
      <label class="inline-flex items-center gap-2 text-chiptext"><input name="live_enabled" type="checkbox" checked /> Live enabled</label>
      <label class="inline-flex items-center gap-2 text-chiptext"><input name="engagement_enabled" type="checkbox" checked /> Engagement enabled</label>
      <Button type="submit">Add channel</Button>
    </form>
  </AdminPanel>
</div>

<AdminPanel title="Source Channels">
  <div class="grid gap-3">
    {#if data.dashboard.sourceChannels.length === 0}
      <EmptyState title="No source channels" message="Add a source channel to let crawler operations pick it up." />
    {:else}
      {#each data.dashboard.sourceChannels as channel (channel.id)}
        <article class="grid items-center gap-3 border-t border-line pt-3 md:grid-cols-[minmax(0,1fr)_auto]">
          <div>
            <strong>{channel.title}</strong>
            <p class="m-0 text-muted">
              {channel.platform}:{channel.platform_id} · {channel.operational_status} · {freshnessCopy(channel)}
            </p>
            <p class="m-0 text-muted">
              {channel.username ?? 'no username'} · session {channel.telegram_session_name ?? 'unassigned'} · catch-up {channel.catchup_enabled ? 'on' : 'off'} / {channel.catchup_message_limit} · live {channel.live_enabled ? 'on' : 'off'} · engagement {channel.engagement_enabled ? 'on' : 'off'}
            </p>
          </div>
          <div class="flex flex-wrap justify-end gap-2">
            {#if channel.is_active}
              <form method="POST" action="?/toggleSourceChannel">
                <input type="hidden" name="channel_id" value={channel.id} />
                <input type="hidden" name="paused" value={channel.is_paused ? 'false' : 'true'} />
                <Button type="submit">{channel.is_paused ? 'Resume' : 'Pause'}</Button>
              </form>
              <form method="POST" action="?/markSourceChannelDead">
                <input type="hidden" name="channel_id" value={channel.id} />
                <Button type="submit" variant="secondary">Mark dead</Button>
              </form>
            {:else}
              <Badge>Removed from crawl</Badge>
            {/if}
          </div>
        </article>
      {/each}
    {/if}
  </div>
</AdminPanel>

<AdminPanel title="Create Meme Template">
  <form method="POST" action="?/createTemplate" class="grid gap-3 md:grid-cols-2">
    <FormRow label="Slug"><Input name="slug" placeholder="drake-hotline-bling" required /></FormRow>
    <FormRow label="Name"><Input name="name" placeholder="Drake Hotline Bling" required /></FormRow>
    <FormRow label="Description" class="md:col-span-2"><Textarea name="description" rows={2} placeholder="Template taxonomy notes" /></FormRow>
    <FormRow label="Base image URL"><Input name="base_image_url" placeholder="https://..." /></FormRow>
    <label class="inline-flex items-center gap-2 text-chiptext"><input name="is_curated" type="checkbox" /> Curated</label>
    <div class="md:col-span-2"><Button type="submit">Create template</Button></div>
  </form>
</AdminPanel>

<AdminPanel title="Meme Templates">
  <div class="grid gap-3">
    {#if data.dashboard.templates.length === 0}
      <EmptyState title="No meme templates" message="Create the first template before assigning memes or merging duplicates." />
    {:else}
      {#each data.dashboard.templates as template (template.id)}
        <article class="grid gap-3 border-t border-line pt-3">
          <form method="POST" action="?/updateTemplate" class="grid gap-2 lg:grid-cols-[1fr_1fr_1.4fr_1.4fr_auto_auto]">
            <input type="hidden" name="template_id" value={template.id} />
            <Input name="slug" value={template.slug} aria-label="Slug" />
            <Input name="name" value={template.name} aria-label="Name" />
            <Input name="description" value={template.description ?? ''} aria-label="Description" />
            <Input name="base_image_url" value={template.base_image_url ?? ''} aria-label="Base image URL" />
            <label class="inline-flex items-center gap-2 text-chiptext"><input name="is_curated" type="checkbox" checked={template.is_curated} /> Curated</label>
            <Button type="submit">Save</Button>
          </form>
          <div class="grid gap-2 rounded-2xl border border-line bg-soft/50 p-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
            <form method="POST" action="?/mergeTemplate" class="grid gap-2">
              <input type="hidden" name="template_id" value={template.id} />
              <strong>Merge duplicate</strong>
              <Select name="target_template_id" aria-label="Target template" required>
                <option value="">Target template</option>
                {#each templateTargets(template.id) as target (target.id)}
                  <option value={target.id}>{target.name} · {target.slug}</option>
                {/each}
              </Select>
              <Input name="confirmation" placeholder="paste source template id" />
              <Input name="note" placeholder="audit note" />
              <Button type="submit" variant="secondary">Merge into target</Button>
            </form>
            <form method="POST" action="?/deleteTemplate" class="grid gap-2">
              <input type="hidden" name="template_id" value={template.id} />
              <strong>Safe delete</strong>
              <p class="m-0 text-sm text-muted">Only succeeds when no memes or moderation history reference this template.</p>
              <Input name="confirmation" placeholder="paste template id" />
              <Input name="note" placeholder="optional note" />
              <Button type="submit" variant="secondary">Delete if unreferenced</Button>
            </form>
          </div>
          <p class="m-0 text-xs text-muted">Template ID: {template.id}</p>
        </article>
      {/each}
    {/if}
  </div>
</AdminPanel>

<AdminPanel title="Moderation Pattern Controls">
  <div class="grid gap-4 lg:grid-cols-[minmax(280px,0.7fr)_minmax(0,1.3fr)]">
    <form method="POST" action="?/createBlockedPerceptualHash" class="grid gap-3 rounded-2xl border border-line bg-soft/40 p-3">
      <strong>Create blocked pHash</strong>
      <FormRow label="Perceptual hash">
        <Input name="perceptual_hash" placeholder="16 hex chars for 64-bit pHash" pattern="[0-9A-Fa-f]+" maxlength={64} required />
      </FormRow>
      <div class="grid gap-3 sm:grid-cols-2">
        <FormRow label="Algorithm"><Input name="hash_algorithm" value="phash" maxlength={32} required /></FormRow>
        <FormRow label="Max distance"><Input name="max_hamming_distance" type="number" min={0} value="0" required /></FormRow>
      </div>
      <div class="grid gap-3 sm:grid-cols-2">
        <FormRow label="Reason">
          <Select name="reason" required>
            {#each moderationReasons as reason}
              <option value={reason}>{reason}</option>
            {/each}
          </Select>
        </FormRow>
        <label class="inline-flex items-center gap-2 text-chiptext"><input name="is_active" type="checkbox" checked /> Active</label>
      </div>
      <FormRow label="Note"><Textarea name="note" rows={2} placeholder="why this pattern is blocked" /></FormRow>
      <Button type="submit">Block pHash</Button>
    </form>

    <div class="grid gap-3">
      {#if data.dashboard.blockedPerceptualHashes.length === 0}
        <EmptyState title="No blocked pHashes" message="Create a pattern to quarantine matching uploads and crawler items during ingest." />
      {:else}
        {#each data.dashboard.blockedPerceptualHashes as blockedHash (blockedHash.id)}
          <article class="grid gap-3 rounded-2xl border border-line bg-paper p-3">
            <div class="flex flex-wrap items-start justify-between gap-2">
              <div>
                <strong class="font-mono text-sm">{blockedHash.perceptual_hash}</strong>
                <p class="m-0 text-muted">
                  {blockedHash.hash_algorithm} · {blockedHash.hash_size} bits · distance &lt;= {blockedHash.max_hamming_distance} · {blockedHash.reason}
                </p>
              </div>
              <Badge>{blockedHash.is_active ? 'active' : 'inactive'}</Badge>
            </div>
            {#if blockedHash.note}
              <p class="m-0 text-sm">{blockedHash.note}</p>
            {/if}
            <form method="POST" action="?/updateBlockedPerceptualHash" class="grid gap-2 lg:grid-cols-[1.3fr_0.7fr_0.5fr_0.7fr_1fr_auto]">
              <input type="hidden" name="blocked_hash_id" value={blockedHash.id} />
              <Input name="perceptual_hash" value={blockedHash.perceptual_hash} aria-label="Perceptual hash" pattern="[0-9A-Fa-f]+" maxlength={64} required />
              <Input name="hash_algorithm" value={blockedHash.hash_algorithm} aria-label="Hash algorithm" maxlength={32} required />
              <Input name="max_hamming_distance" type="number" min={0} value={blockedHash.max_hamming_distance} aria-label="Max Hamming distance" required />
              <Select name="reason" aria-label="Reason">
                {#each moderationReasons as reason}
                  <option value={reason} selected={reason === blockedHash.reason}>{reason}</option>
                {/each}
              </Select>
              <Input name="note" value={blockedHash.note ?? ''} aria-label="Note" />
              <label class="inline-flex items-center gap-2 text-chiptext"><input name="is_active" type="checkbox" checked={blockedHash.is_active} /> Active</label>
              <div class="lg:col-span-6"><Button type="submit">Save blocked pHash</Button></div>
            </form>
            <div class="flex flex-wrap gap-2">
              {#if blockedHash.is_active}
                <form method="POST" action="?/deactivateBlockedPerceptualHash" class="flex flex-wrap gap-2">
                  <input type="hidden" name="blocked_hash_id" value={blockedHash.id} />
                  <Input name="note" placeholder="deactivation note" />
                  <Button type="submit" variant="secondary">Deactivate</Button>
                </form>
              {/if}
              <form method="POST" action="?/deleteBlockedPerceptualHash">
                <input type="hidden" name="blocked_hash_id" value={blockedHash.id} />
                <Button type="submit" variant="secondary">Delete if unreferenced</Button>
              </form>
            </div>
            <p class="m-0 text-xs text-muted">Blocked pHash ID: {blockedHash.id}</p>
          </article>
        {/each}
      {/if}
    </div>
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
