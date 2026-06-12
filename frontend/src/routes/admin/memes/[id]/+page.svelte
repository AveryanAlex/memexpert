<script lang="ts">
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

<section class="admin-hero">
  <p><a class="download-link" href="/admin">Back to admin list</a></p>
  <p class="pill">Signed in as {data.adminUser.email || data.adminUser.id}</p>
  <h1>Meme detail</h1>
  <p class="muted">Review reports, override visibility/classification, and inspect audit history.</p>
</section>

{#if form?.message}
  <p class="notice" role="status">{form.message}</p>
{/if}

{#if data.loadError}
  <p class="notice" role="alert">{data.loadError}</p>
{/if}

{#if data.detail}
  {@const detail = data.detail}
  <div class="admin-grid detail-admin-grid">
    <section class="admin-panel">
      <h2>Metadata</h2>
      <dl class="admin-meta-list">
        <div><dt>ID</dt><dd>{detail.meme.id}</dd></div>
        <div><dt>Media</dt><dd>{detail.meme.media_type}</dd></div>
        <div><dt>Language</dt><dd>{detail.meme.language}</dd></div>
        <div><dt>Score</dt><dd>{detail.meme.popularity_score.toFixed(1)}</dd></div>
        <div><dt>Likes</dt><dd>{detail.meme.like_count}</dd></div>
        <div><dt>Author</dt><dd>{detail.meme.author_user_id ?? 'none'}</dd></div>
        <div><dt>Tags</dt><dd>{detail.meme.tags.length ? detail.meme.tags.join(', ') : 'none'}</dd></div>
        <div><dt>Created</dt><dd>{detail.meme.created_at}</dd></div>
      </dl>
    </section>

    <section class="admin-panel">
      <h2>Overrides</h2>
      <form method="POST" action="?/updateMeme" class="admin-form">
        <label class="checkbox-row"><input name="is_public" type="checkbox" checked={detail.meme.is_public} /> Public</label>
        <label class="checkbox-row"><input name="is_nsfw" type="checkbox" checked={detail.meme.is_nsfw} /> NSFW</label>
        <label>
          Template
          <select name="template_id">
            <option value="" selected={detail.meme.template_id === null}>No template</option>
            {#each data.templates as template (template.id)}
              <option value={template.id} selected={template.id === detail.meme.template_id}>{template.name}</option>
            {/each}
          </select>
        </label>
        <label>
          Reason
          <select name="reason">
            <option value="">No reason</option>
            {#each moderationReasons as reason}
              <option value={reason}>{reason}</option>
            {/each}
          </select>
        </label>
        <input name="note" placeholder="audit note" />
        <button type="submit">Save overrides</button>
      </form>

      <div class="destructive-actions" aria-label="Destructive actions">
        <h3>Destructive actions</h3>
        <p class="warning-copy">Irreversible. These actions record a durable admin audit before the source meme row is removed.</p>

        <form method="POST" action="?/deleteMeme" class="admin-form destructive-form">
          <strong>Delete meme</strong>
          <p class="muted">Removes the meme and cascaded dependents after snapshotting files, SEO, saves, pins, reports, decisions, popularity, and sync state.</p>
          <label>
            Type meme id to confirm
            <input name="confirmation" placeholder={detail.meme.id} required />
          </label>
          <label>
            Required audit note
            <input name="note" placeholder="why this deletion is necessary" required />
          </label>
          <button type="submit" class="danger-button">Delete permanently</button>
        </form>

        <form method="POST" action="?/mergeMeme" class="admin-form destructive-form">
          <strong>Merge into another meme</strong>
          <p class="muted">Transfers files, collection saves, pins, and popularity lineage into the target, then deletes this source meme.</p>
          <label>
            Target meme id
            <input name="target_meme_id" placeholder="target UUID" required />
          </label>
          <label>
            Type source meme id to confirm
            <input name="confirmation" placeholder={detail.meme.id} required />
          </label>
          <label>
            Required audit note
            <input name="note" placeholder="why these memes should be merged" required />
          </label>
          <button type="submit" class="danger-button">Merge into target</button>
        </form>
      </div>
    </section>
  </div>

  <section class="admin-panel">
    <h2>Reports</h2>
    {#if detail.reports.length === 0}
      <p class="muted">No reports for this meme.</p>
    {:else}
      <div class="admin-list">
        {#each detail.reports as report (report.id)}
          <article class="admin-row moderation-report-row">
            <div>
              <strong>{report.reason.toUpperCase()} report</strong>
              <p class="muted">{report.status} - reporter {report.reporter_user_id ?? 'anonymous'} - {report.created_at}</p>
              {#if report.note}<p>{report.note}</p>{/if}
              {#if report.resolved_at}<p class="muted">Resolved {report.resolved_at} by {report.resolved_by_admin_user_id ?? 'unknown'}</p>{/if}
            </div>
            <form method="POST" action="?/resolveReport" class="inline-form moderation-form">
              <input type="hidden" name="report_id" value={report.id} />
              <select name="action" aria-label="Resolution action" disabled={report.status !== 'pending' && report.status !== 'in_review'}>
                {#each reportActions as [value, label]}
                  <option value={value}>{label}</option>
                {/each}
              </select>
              <select name="reason" aria-label="Decision reason" disabled={report.status !== 'pending' && report.status !== 'in_review'}>
                {#each moderationReasons as reason}
                  <option value={reason} selected={reason === report.reason}>{reason}</option>
                {/each}
              </select>
              <input name="note" placeholder="decision note" disabled={report.status !== 'pending' && report.status !== 'in_review'} />
              <button type="submit" disabled={report.status !== 'pending' && report.status !== 'in_review'}>Resolve</button>
            </form>
          </article>
        {/each}
      </div>
    {/if}
  </section>

  <section class="admin-panel">
    <h2>Decision History</h2>
    {#if detail.decisions.length === 0}
      <p class="muted">No admin changes recorded yet.</p>
    {:else}
      <div class="admin-list">
        {#each detail.decisions as decision (decision.id)}
          <article class="audit-row">
            <div>
              <strong>{decision.action}</strong>
              <p class="muted">{decision.created_at} - admin {decision.admin_user_id ?? 'unknown'}</p>
            </div>
            <code>
              public {decision.previous_is_public ? 'yes' : 'no'} -> {decision.new_is_public ? 'yes' : 'no'};
              nsfw {decision.previous_is_nsfw ? 'yes' : 'no'} -> {decision.new_is_nsfw ? 'yes' : 'no'};
              template {decision.previous_template_id ?? 'none'} -> {decision.new_template_id ?? 'none'}
            </code>
            {#if decision.note}<p>{decision.note}</p>{/if}
          </article>
        {/each}
      </div>
    {/if}
  </section>
{/if}
