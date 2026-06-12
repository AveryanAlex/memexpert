<script lang="ts">
  import MemeCard from '$lib/components/MemeCard.svelte';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form?: ActionData } = $props();

  const detail = $derived(data.detail);
  const collection = $derived(detail?.collection ?? null);
  const capabilities = $derived(detail?.capabilities ?? null);
  const isActive = $derived(Boolean(detail && detail.active_save_collection_id === detail.collection.id));
  const memberCount = $derived(collection?.memberships.length ?? 0);

  function formatDate(value: string): string {
    return new Intl.DateTimeFormat('en', { dateStyle: 'medium' }).format(new Date(value));
  }
</script>

{#if detail && collection && capabilities}
  <section class="collection-hero" aria-labelledby="collection-title">
    <div>
      <a class="eyebrow-link" href="/">Back to catalog</a>
      <h1 id="collection-title">{collection.title}</h1>
      <p class="muted">
        {collection.description || 'No description yet.'}
      </p>
      <div class="meta collection-meta" aria-label="Collection metadata">
        <span>{collection.kind}</span>
        <span>{collection.visibility}</span>
        <span>{detail.viewer_role}</span>
        <span>{memberCount} member{memberCount === 1 ? '' : 's'}</span>
        <span>Updated {formatDate(collection.updated_at)}</span>
      </div>
    </div>

    <aside class="collection-actions-card">
      <p class="account-title">Save destination</p>
      <p class="account-copy">
        {isActive ? 'New Save actions go here.' : 'Make this your active collection for Save actions.'}
      </p>
      {#if capabilities.can_set_active_save}
        <form method="POST" action="?/setActive">
          <button class="button-link compact" type="submit" disabled={isActive}>{isActive ? 'Active now' : 'Set active'}</button>
        </form>
      {/if}
    </aside>
  </section>

  {#if form?.errorMessage}
    <p class="notice" role="alert">{form.errorMessage}</p>
  {:else if form?.successMessage}
    <p class="notice" role="status">{form.successMessage}</p>
  {/if}

  {#if form?.inviteUrl}
    <section class="notice" aria-labelledby="invite-created-title">
      <h2 id="invite-created-title">Invite link ready</h2>
      <p class="muted">Share this link with a full MemeXpert account. It is shown once.</p>
      <input class="copy-field" value={form.inviteUrl} readonly aria-label="Invite link" />
    </section>
  {/if}

  <section class="collection-panels" aria-label="Collection controls">
    {#if capabilities.can_rename}
      <form class="admin-panel admin-form" method="POST" action="?/update">
        <h2>Collection details</h2>
        <input name="title" value={collection.title} maxlength="120" required aria-label="Collection title" />
        <input name="description" value={collection.description ?? ''} aria-label="Collection description" placeholder="Description" />
        <select name="visibility" aria-label="Collection visibility">
          <option value="private" selected={collection.visibility === 'private'}>Private</option>
          <option value="unlisted" selected={collection.visibility === 'unlisted'}>Unlisted</option>
          <option value="public" selected={collection.visibility === 'public'}>Public</option>
        </select>
        <button type="submit">Save changes</button>
      </form>
    {/if}

    {#if capabilities.can_create_invites}
      <form class="admin-panel admin-form" method="POST" action="?/createInvite">
        <h2>Invite link</h2>
        <input name="label" placeholder="Optional label" aria-label="Invite label" />
        <select name="role" aria-label="Invite role">
          <option value="viewer">Viewer</option>
          <option value="editor">Editor</option>
        </select>
        <input name="max_uses" type="number" min="1" value="1" aria-label="Maximum uses" />
        <input name="expires_in_hours" type="number" min="1" max="720" value="168" aria-label="Expires in hours" />
        <button type="submit">Create invite</button>
      </form>
    {/if}

    {#if capabilities.can_delete}
      <form class="admin-panel admin-form danger-panel" method="POST" action="?/delete">
        <h2>Danger zone</h2>
        <p class="muted">Delete this custom collection and remove its saved meme rows. The memes themselves stay in the catalog.</p>
        <button type="submit">Delete collection</button>
      </form>
    {/if}
  </section>

  <div class="status-row">
    <p class="muted">{detail.saved_memes.length} saved meme{detail.saved_memes.length === 1 ? '' : 's'}</p>
    <p class="muted">Remove controls appear for writable collections.</p>
  </div>

  {#if detail.saved_memes.length > 0}
    <section class="grid" aria-label="Saved memes">
      {#each detail.saved_memes as item (item.save.meme_id)}
        <div class="collection-card-wrap">
          <MemeCard meme={item.meme} />
          {#if capabilities.can_remove_memes}
            <form class="remove-save-form" method="POST" action="?/removeMeme">
              <input type="hidden" name="meme_id" value={item.save.meme_id} />
              <button class="button-link compact secondary" type="submit">Remove</button>
            </form>
          {/if}
        </div>
      {/each}
    </section>
  {:else}
    <section class="empty-state">
      <h2>No saved memes yet</h2>
      <p class="muted">
        {isActive ? 'Browse the catalog and use Save to add memes here.' : 'Set this collection active, then browse and save memes into it.'}
      </p>
      <a class="button-link compact" href="/">Browse memes</a>
    </section>
  {/if}
{:else}
  <section class="empty-state">
    <h1>Collection unavailable</h1>
    <p class="muted">{data.errorMessage ?? 'You may need to join this collection or sign in with an account that has access.'}</p>
    <a class="button-link compact" href="/">Back to catalog</a>
  </section>
{/if}
