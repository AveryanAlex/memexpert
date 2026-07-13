<script lang="ts">
  import { readAuthState } from '$lib/auth-state';
  import CollectionManagement from '$lib/features/collections/CollectionManagement.svelte';
  import { collectionAccessSummary } from '$lib/features/collections/view-model';
  import MemeGrid from '$lib/features/memes/MemeGrid.svelte';
  import { ActionLink, Badge, Button, Card, EmptyState, Notice } from '$lib/ui';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form?: ActionData } = $props();

  const authState = readAuthState(() => ({ session: data.session ?? null, sessionError: data.sessionError }));
  const session = $derived($authState.session);

  const detail = $derived(data.detail);
  const collection = $derived(detail?.collection ?? null);
  const capabilities = $derived(detail?.capabilities ?? null);
  const isActive = $derived(Boolean(detail && detail.active_save_collection_id === detail.collection.id));
  const memberCount = $derived(collection?.memberships.length ?? 0);
  const accessSummary = $derived(detail ? collectionAccessSummary(detail) : '');
  const savedMemes = $derived(detail?.saved_memes.map((item) => item.meme) ?? []);

  function formatDate(value: string): string {
    return `${new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeZone: 'UTC' }).format(new Date(value))} UTC`;
  }
</script>

{#if detail && collection && capabilities}
  <section class="mb-6 grid items-start gap-5 md:grid-cols-[minmax(0,1fr)_minmax(230px,0.35fr)]" aria-labelledby="collection-title">
    <div>
      <a class="text-sm font-black text-muted underline decoration-2 underline-offset-4" href="/">Back to catalog</a>
      <h1 id="collection-title" class="mb-2 mt-2 text-3xl font-black tracking-[-0.05em] sm:text-4xl">{collection.title}</h1>
      <p class="m-0 text-muted">
        {collection.description || 'No description yet.'}
      </p>
      <p class="m-0 mt-3 text-sm text-muted">{accessSummary}</p>
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
      {:else if session?.user.account_type === 'guest'}
        <ActionLink size="compact" href="/account/telegram?returnTo=/profile">Connect for custom saves</ActionLink>
      {:else}
        <p class="m-0 text-sm text-muted">You need editor or owner access to make this the save destination.</p>
      {/if}
    </Card>
  </section>

  {#if form?.errorMessage}
    <Notice role="alert" tone="danger">{form.errorMessage}</Notice>
  {:else if form?.successMessage}
    <Notice>{form.successMessage}</Notice>
  {/if}

  <section class="my-7" aria-labelledby="saved-memes-title">
    <div class="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h2 id="saved-memes-title" class="m-0 text-2xl font-black tracking-[-0.04em]">Saved memes</h2>
        <p class="m-0 mt-1 text-muted">{detail.saved_memes.length} saved meme{detail.saved_memes.length === 1 ? '' : 's'}</p>
      </div>
      <p class="m-0 text-sm text-muted">Use bulk selection to download or remove saved memes when allowed.</p>
    </div>

    {#if detail.saved_memes.length > 0}
      <MemeGrid
        memes={savedMemes}
        label="Saved memes"
        bulk={{
          enabled: true,
          removeCollectionId: collection.id,
          removeEnabled: capabilities.can_remove_memes,
          guidance: capabilities.can_remove_memes
            ? 'Editors and owners can remove selected memes from this collection.'
            : session?.user.account_type === 'guest'
              ? 'Guests can browse and favorite. Connect Telegram for collection collaboration actions.'
              : 'Your role can view this collection but cannot remove saved memes.'
        }}
        showAccessMarkers={Boolean(session)}
      />
    {:else}
      <EmptyState title="No saved memes yet" message={isActive ? 'Browse the catalog and use Save to add memes here.' : 'Set this collection active, then browse and save memes into it.'}>
        <ActionLink size="compact" href="/">Browse memes</ActionLink>
      </EmptyState>
    {/if}
  </section>

  <CollectionManagement {detail} {session} loadedAt={data.loadedAt} inviteUrl={form?.inviteUrl} />
{:else}
  <EmptyState title="Collection unavailable" message={data.errorMessage ?? 'You may need to join this collection or sign in with an account that has access.'}>
    <ActionLink size="compact" href="/">Back to catalog</ActionLink>
  </EmptyState>
{/if}
