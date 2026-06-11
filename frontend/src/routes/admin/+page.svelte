<script lang="ts">
  import type { PageData } from './$types';
  import type { ActionData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<section class="admin-hero">
  <div>
    <p class="pill">Signed in as {data.adminUser.email || data.adminUser.id}</p>
    <h1>Admin tools</h1>
    <p class="muted">Initial browser-safe controls for source curation, templates, and moderation flags.</p>
  </div>
</section>

{#if form?.message}
  <p class="notice" role="status">{form.message}</p>
{/if}

{#if data.loadError}
  <p class="notice" role="alert">{data.loadError}</p>
{/if}

<div class="admin-grid">
  <section class="admin-panel">
    <h2>Channel Suggestions</h2>
    {#if data.dashboard.suggestions.length === 0}
      <p class="muted">No suggestions yet.</p>
    {:else}
      {#each data.dashboard.suggestions as suggestion (suggestion.id)}
        <article class="admin-row">
          <div>
            <strong>{suggestion.channel_url}</strong>
            <p class="muted">{suggestion.platform} · {suggestion.status}</p>
          </div>
          <form method="POST" action="?/reviewSuggestion" class="inline-form">
            <input type="hidden" name="suggestion_id" value={suggestion.id} />
            <input name="admin_note" placeholder="note" value={suggestion.admin_note ?? ''} />
            <button name="decision" value="approve" type="submit">Approve</button>
            <button name="decision" value="reject" type="submit" class="secondary-button">Reject</button>
          </form>
        </article>
      {/each}
    {/if}
  </section>

  <section class="admin-panel">
    <h2>Add Source Channel</h2>
    <form method="POST" action="?/addSourceChannel" class="admin-form">
      <select name="platform" aria-label="Platform">
        <option value="telegram">Telegram</option>
        <option value="reddit">Reddit</option>
        <option value="vk">VK</option>
      </select>
      <input name="platform_id" placeholder="platform id" required />
      <input name="title" placeholder="title" required />
      <input name="username" placeholder="username" />
      <input name="session_id" placeholder="session" />
      <input name="catchup_message_limit" type="number" min="1" max="10000" value="500" />
      <label class="checkbox-row"><input name="catchup_enabled" type="checkbox" checked /> Catch-up enabled</label>
      <button type="submit">Add channel</button>
    </form>
  </section>
</div>

<section class="admin-panel">
  <h2>Source Channels</h2>
  <div class="admin-list">
    {#each data.dashboard.sourceChannels as channel (channel.id)}
      <article class="admin-row">
        <div>
          <strong>{channel.title}</strong>
          <p class="muted">{channel.platform}:{channel.platform_id} · {channel.is_paused ? 'paused' : 'active'}</p>
        </div>
        <form method="POST" action="?/toggleSourceChannel">
          <input type="hidden" name="channel_id" value={channel.id} />
          <input type="hidden" name="paused" value={channel.is_paused ? 'false' : 'true'} />
          <button type="submit">{channel.is_paused ? 'Resume' : 'Pause'}</button>
        </form>
      </article>
    {/each}
  </div>
</section>

<section class="admin-panel">
  <h2>Meme Templates</h2>
  <div class="admin-list">
    {#each data.dashboard.templates as template (template.id)}
      <form method="POST" action="?/updateTemplate" class="template-form">
        <input type="hidden" name="template_id" value={template.id} />
        <input name="slug" value={template.slug} aria-label="Slug" />
        <input name="name" value={template.name} aria-label="Name" />
        <input name="description" value={template.description ?? ''} aria-label="Description" />
        <input name="base_image_url" value={template.base_image_url ?? ''} aria-label="Base image URL" />
        <label class="checkbox-row"><input name="is_curated" type="checkbox" checked={template.is_curated} /> Curated</label>
        <button type="submit">Save</button>
      </form>
    {/each}
  </div>
</section>

<section class="admin-panel">
  <h2>Meme Moderation</h2>
  <p class="muted">Current model support is limited to direct public and NSFW flag overrides.</p>
  <div class="admin-list">
    {#each data.dashboard.memes as meme (meme.id)}
      <form method="POST" action="?/updateMemeModeration" class="admin-row">
        <input type="hidden" name="meme_id" value={meme.id} />
        <div>
          <strong>{meme.id}</strong>
          <p class="muted">{meme.media_type} · {meme.language} · score {meme.popularity_score.toFixed(1)}</p>
        </div>
        <label class="checkbox-row"><input name="is_public" type="checkbox" checked={meme.is_public} /> Public</label>
        <label class="checkbox-row"><input name="is_nsfw" type="checkbox" checked={meme.is_nsfw} /> NSFW</label>
        <button type="submit">Update</button>
      </form>
    {/each}
  </div>
</section>
