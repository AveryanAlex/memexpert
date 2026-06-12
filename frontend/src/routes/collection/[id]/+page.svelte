<script lang="ts">
  import MemeCard from '$lib/features/memes/MemeCard.svelte';
  import { ActionLink, Badge, Button, Card, EmptyState, Input, Notice, Select } from '$lib/ui';
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
  <section class="mb-5 grid items-start gap-5 md:grid-cols-[minmax(0,1fr)_minmax(230px,0.35fr)]" aria-labelledby="collection-title">
    <div>
      <a class="text-sm font-black text-muted underline decoration-2 underline-offset-4" href="/">Back to catalog</a>
      <h1 id="collection-title" class="mb-3 mt-2 text-[clamp(2.4rem,8vw,5.2rem)] font-black leading-[0.9] tracking-[-0.075em]">{collection.title}</h1>
      <p class="m-0 text-muted">
        {collection.description || 'No description yet.'}
      </p>
      <div class="mt-4 flex flex-wrap gap-2" aria-label="Collection metadata">
        <Badge>{collection.kind}</Badge>
        <Badge>{collection.visibility}</Badge>
        <Badge>{detail.viewer_role}</Badge>
        <Badge>{memberCount} member{memberCount === 1 ? '' : 's'}</Badge>
        <Badge>Updated {formatDate(collection.updated_at)}</Badge>
      </div>
    </div>

    <Card class="grid gap-3 shadow-none">
      <p class="m-0 font-black">Save destination</p>
      <p class="m-0 text-sm text-muted">
        {isActive ? 'New Save actions go here.' : 'Make this your active collection for Save actions.'}
      </p>
      {#if capabilities.can_set_active_save}
        <form method="POST" action="?/setActive">
          <Button size="compact" type="submit" disabled={isActive}>{isActive ? 'Active now' : 'Set active'}</Button>
        </form>
      {/if}
    </Card>
  </section>

  {#if form?.errorMessage}
    <Notice role="alert" tone="danger">{form.errorMessage}</Notice>
  {:else if form?.successMessage}
    <Notice>{form.successMessage}</Notice>
  {/if}

  {#if form?.inviteUrl}
    <Card class="my-4 grid gap-2" aria-labelledby="invite-created-title">
      <h2 id="invite-created-title" class="m-0 text-2xl font-black tracking-[-0.04em]">Invite link ready</h2>
      <p class="m-0 text-muted">Share this link with a full MemeXpert account. It is shown once.</p>
      <Input value={form.inviteUrl} readonly aria-label="Invite link" />
    </Card>
  {/if}

  <section class="my-5 grid gap-4 md:grid-cols-3" aria-label="Collection controls">
    {#if capabilities.can_rename}
      <Card class="grid gap-3 shadow-none">
        <form class="grid gap-3" method="POST" action="?/update">
          <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Collection details</h2>
          <Input name="title" value={collection.title} maxlength={120} required aria-label="Collection title" />
          <Input name="description" value={collection.description ?? ''} aria-label="Collection description" placeholder="Description" />
          <Select name="visibility" aria-label="Collection visibility">
            <option value="private" selected={collection.visibility === 'private'}>Private</option>
            <option value="unlisted" selected={collection.visibility === 'unlisted'}>Unlisted</option>
            <option value="public" selected={collection.visibility === 'public'}>Public</option>
          </Select>
          <Button type="submit">Save changes</Button>
        </form>
      </Card>
    {/if}

    {#if capabilities.can_create_invites}
      <Card class="grid gap-3 shadow-none">
        <form class="grid gap-3" method="POST" action="?/createInvite">
          <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Invite link</h2>
          <Input name="label" placeholder="Optional label" aria-label="Invite label" />
          <Select name="role" aria-label="Invite role">
            <option value="viewer">Viewer</option>
            <option value="editor">Editor</option>
          </Select>
          <Input name="max_uses" type="number" min="1" value="1" aria-label="Maximum uses" />
          <Input name="expires_in_hours" type="number" min="1" max="720" value="168" aria-label="Expires in hours" />
          <Button type="submit">Create invite</Button>
        </form>
      </Card>
    {/if}

    {#if capabilities.can_delete}
      <form class="grid gap-3 rounded-[28px] border border-danger-line bg-danger-surface p-6" method="POST" action="?/delete">
        <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Danger zone</h2>
        <p class="m-0 text-muted">Delete this custom collection and remove its saved meme rows. The memes themselves stay in the catalog.</p>
        <Button variant="danger" type="submit">Delete collection</Button>
      </form>
    {/if}
  </section>

  <div class="my-7 flex flex-wrap justify-between gap-3">
    <p class="m-0 text-muted">{detail.saved_memes.length} saved meme{detail.saved_memes.length === 1 ? '' : 's'}</p>
    <p class="m-0 text-muted">Remove controls appear for writable collections.</p>
  </div>

  {#if detail.saved_memes.length > 0}
    <section class="grid grid-cols-1 gap-4 md:grid-cols-3" aria-label="Saved memes">
      {#each detail.saved_memes as item (item.save.meme_id)}
        <div class="relative">
          <MemeCard meme={item.meme} />
          {#if capabilities.can_remove_memes}
            <form class="absolute bottom-3 right-3 z-10" method="POST" action="?/removeMeme">
              <input type="hidden" name="meme_id" value={item.save.meme_id} />
              <Button size="compact" variant="secondary" type="submit">Remove</Button>
            </form>
          {/if}
        </div>
      {/each}
    </section>
  {:else}
    <EmptyState title="No saved memes yet" message={isActive ? 'Browse the catalog and use Save to add memes here.' : 'Set this collection active, then browse and save memes into it.'}>
      <ActionLink size="compact" href="/">Browse memes</ActionLink>
    </EmptyState>
  {/if}
{:else}
  <EmptyState title="Collection unavailable" message={data.errorMessage ?? 'You may need to join this collection or sign in with an account that has access.'}>
    <ActionLink size="compact" href="/">Back to catalog</ActionLink>
  </EmptyState>
{/if}
