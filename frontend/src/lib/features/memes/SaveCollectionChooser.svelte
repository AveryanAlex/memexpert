<script lang="ts">
  import { browser } from '$app/environment';
  import { uuidV7 } from '$lib/analytics/uuid-v7';
  import { fetchMemeCollectionChoices, removeMemeFromCollection, saveMemeToCollection } from '$lib/api/client';
  import type { MemeCollectionChoiceRead } from '$lib/api/types';
  import { actionFailureMessage, memeActionAttributionBody, type MemeActionAttribution } from '$lib/memeActions';
  import * as Popover from '$lib/ui/popover';
  import { cn, focusRing } from '$lib/ui/styles';
  import { Bookmark, Check, LoaderCircle, LockKeyhole, Plus, X } from '@lucide/svelte';
  import { createSaveCollectionRequestGate } from './save-collection-chooser-state';

  interface Props {
    memeId: string;
    title: string;
    attribution?: MemeActionAttribution | null;
    surface: 'card' | 'detail';
    saved: boolean;
    savedCollectionIds?: readonly string[];
    disabled?: boolean;
    onMembershipChange: (collectionIds: readonly string[]) => void;
  }

  let {
    memeId,
    title,
    attribution = null,
    surface,
    saved,
    savedCollectionIds,
    disabled = false,
    onMembershipChange
  }: Props = $props();
  const componentId = $props.id();
  const requestGate = createSaveCollectionRequestGate();

  let open = $state(false);
  let requestForOpen = $state<string | null>(null);
  let loading = $state(false);
  let ready = $state(false);
  let errorMessage = $state<string | null>(null);
  let pendingCollectionId = $state<string | null>(null);
  let choices = $state<MemeCollectionChoiceRead[]>([]);

  const isCardSurface = $derived(surface === 'card');
  const savedIds = $derived(
    new Set(savedCollectionIds ?? choices.filter((choice) => choice.contains_meme).map((choice) => choice.collection_id))
  );
  const savedChoices = $derived(choices.filter((choice) => savedIds.has(choice.collection_id)));
  const availableChoices = $derived(
    choices.filter((choice) => !savedIds.has(choice.collection_id) && choice.can_add_memes)
  );

  $effect(() => {
    const currentMemeId = memeId;
    if (requestGate.reset(currentMemeId)) {
      requestForOpen = null;
      loading = false;
      ready = false;
      errorMessage = null;
      pendingCollectionId = null;
      choices = [];
    }

    if (open && requestForOpen !== currentMemeId) {
      requestForOpen = currentMemeId;
      void loadChoices(currentMemeId);
    } else if (!open && requestForOpen !== null) {
      requestForOpen = null;
    }
  });

  async function loadChoices(targetMemeId: string) {
    if (!browser) return;

    const requestToken = requestGate.beginLoad(targetMemeId, savedCollectionIds);
    loading = true;
    ready = false;
    errorMessage = null;
    choices = [];
    try {
      const response = await fetchMemeCollectionChoices({
        fetch,
        baseUrl: window.location.origin,
        memeId: targetMemeId
      });

      if (!requestGate.isLatestLoad(requestToken, memeId)) return;
      if (requestGate.membershipChanged(requestToken, savedCollectionIds)) {
        // Another copy of this meme (for example Meme of the Day) changed membership while
        // this GET was in flight. Never republish the stale snapshot; refresh if still open.
        if (open) void loadChoices(targetMemeId);
        return;
      }

      choices = response.collections;
      ready = true;
      onMembershipChange(
        response.collections.filter((choice) => choice.contains_meme).map((choice) => choice.collection_id)
      );
    } catch (error) {
      if (!requestGate.isLatestLoad(requestToken, memeId)) return;
      errorMessage = error instanceof Error ? error.message : 'Could not load your collections.';
    } finally {
      if (requestGate.isLatestLoad(requestToken, memeId)) loading = false;
    }
  }

  async function toggleChoice(choice: MemeCollectionChoiceRead) {
    if (!browser || pendingCollectionId) return;

    const targetMemeId = memeId;
    const containsMeme = savedIds.has(choice.collection_id);
    if ((containsMeme && !choice.can_remove_memes) || (!containsMeme && !choice.can_add_memes)) return;

    const mutationToken = requestGate.beginMutation(targetMemeId);
    const currentSavedIds = [...savedIds];
    pendingCollectionId = choice.collection_id;
    loading = false;
    errorMessage = null;
    try {
      const actionBody = memeActionAttributionBody(attribution, uuidV7());
      if (containsMeme) {
        await removeMemeFromCollection({
          fetch,
          baseUrl: window.location.origin,
          collectionId: choice.collection_id,
          memeId: targetMemeId,
          body: actionBody
        });
      } else {
        await saveMemeToCollection({
          fetch,
          baseUrl: window.location.origin,
          collectionId: choice.collection_id,
          memeId: targetMemeId,
          body: actionBody
        });
      }

      if (!requestGate.isLatestMutation(mutationToken, memeId)) return;
      const nextIds = containsMeme
        ? currentSavedIds.filter((collectionId) => collectionId !== choice.collection_id)
        : [choice.collection_id, ...currentSavedIds.filter((collectionId) => collectionId !== choice.collection_id)];
      const updatedChoice = { ...choice, contains_meme: !containsMeme };
      choices = containsMeme
        ? choices.map((current) => (current.collection_id === choice.collection_id ? updatedChoice : current))
        : [updatedChoice, ...choices.filter((current) => current.collection_id !== choice.collection_id)];
      onMembershipChange(nextIds);
    } catch (error) {
      if (!requestGate.isLatestMutation(mutationToken, memeId)) return;
      errorMessage = actionFailureMessage(containsMeme ? 'unsave' : 'save', error);
    } finally {
      if (requestGate.isLatestMutation(mutationToken, memeId)) pendingCollectionId = null;
    }
  }

  function choiceLabel(choice: MemeCollectionChoiceRead): string {
    if (!savedIds.has(choice.collection_id)) return `Add to ${choice.title}`;
    return choice.can_remove_memes ? `Remove from ${choice.title}` : `Saved in ${choice.title} (read only)`;
  }
</script>

<Popover.Root bind:open>
  <Popover.Trigger
    variant={isCardSurface ? 'ghost' : 'secondary'}
    class={isCardSurface ? 'h-10 w-full min-w-0 px-0 py-0' : ''}
    aria-label="Save to collection"
    title="Save to collection"
    aria-pressed={saved}
    disabled={disabled || pendingCollectionId !== null}
  >
    <Bookmark class={cn(isCardSurface ? 'size-5' : 'size-4', saved && 'fill-current text-accent')} aria-hidden="true" />
    {#if !isCardSurface}{saved ? 'Saved' : 'Save'}{/if}
  </Popover.Trigger>

  <Popover.Content
    variant="floating"
    align={isCardSurface ? 'center' : 'start'}
    class="max-h-[min(28rem,calc(100dvh-2rem))] w-[min(20rem,calc(100vw-2rem))] overflow-y-auto p-2"
    aria-label={`Collections for ${title}`}
  >
    <div class="px-2 pb-1 pt-1">
      <p class="m-0 text-sm font-extrabold text-ink">Save to collection</p>
      <p class="m-0 text-xs text-muted">Choose every collection where this meme belongs.</p>
    </div>

    {#if loading}
      <div class="flex items-center gap-2 rounded-xl px-3 py-3 text-sm font-semibold text-muted">
        <LoaderCircle class="size-4 animate-spin" aria-hidden="true" />
        Loading collections…
      </div>
    {:else}
      {#if savedChoices.length > 0}
        <section class="grid gap-1" aria-labelledby={`${componentId}-saved-collections`}>
          <p id={`${componentId}-saved-collections`} class="m-0 px-2 pt-1 text-[0.68rem] font-black uppercase tracking-[0.14em] text-muted">Saved in</p>
          {#each savedChoices as choice (choice.collection_id)}
            <button
              class={cn(
                'flex w-full items-center gap-2 rounded-xl bg-soft px-3 py-2.5 text-left font-bold text-ink transition hover:bg-cream disabled:cursor-not-allowed disabled:opacity-70',
                focusRing
              )}
              type="button"
              aria-label={choiceLabel(choice)}
              aria-pressed="true"
              disabled={pendingCollectionId !== null || !choice.can_remove_memes}
              onclick={() => toggleChoice(choice)}
            >
              {#if pendingCollectionId === choice.collection_id}
                <LoaderCircle class="size-4 shrink-0 animate-spin text-accent" aria-hidden="true" />
              {:else}
                <Check class="size-4 shrink-0 text-accent" aria-hidden="true" />
              {/if}
              <span class="min-w-0 flex-1 truncate">{choice.title}</span>
              {#if choice.can_remove_memes}
                <X class="size-4 shrink-0 text-muted" aria-hidden="true" />
              {:else}
                <LockKeyhole class="size-4 shrink-0 text-muted" aria-hidden="true" />
              {/if}
            </button>
          {/each}
        </section>
      {/if}

      {#if availableChoices.length > 0}
        <section class="grid gap-1" aria-labelledby={`${componentId}-available-collections`}>
          <p id={`${componentId}-available-collections`} class="m-0 px-2 pt-2 text-[0.68rem] font-black uppercase tracking-[0.14em] text-muted">Add to</p>
          {#each availableChoices as choice (choice.collection_id)}
            <button
              class={cn(
                'flex w-full items-center gap-2 rounded-xl px-3 py-2.5 text-left font-bold text-ink transition hover:bg-soft disabled:cursor-progress disabled:opacity-70',
                focusRing
              )}
              type="button"
              aria-label={choiceLabel(choice)}
              aria-pressed="false"
              disabled={pendingCollectionId !== null}
              onclick={() => toggleChoice(choice)}
            >
              {#if pendingCollectionId === choice.collection_id}
                <LoaderCircle class="size-4 shrink-0 animate-spin text-accent" aria-hidden="true" />
              {:else}
                <Plus class="size-4 shrink-0 text-accent" aria-hidden="true" />
              {/if}
              <span class="min-w-0 flex-1 truncate">{choice.title}</span>
            </button>
          {/each}
        </section>
      {/if}

      {#if ready && savedChoices.length === 0 && availableChoices.length === 0}
        <div class="grid gap-2 rounded-xl px-3 py-3 text-sm text-muted">
          <p class="m-0">No non-Favorites collections are available yet.</p>
          <a class="font-bold text-ink" href="/library">Manage collections</a>
        </div>
      {/if}
    {/if}

    {#if errorMessage}
      <p class="m-0 rounded-xl px-3 py-2 text-sm font-semibold text-danger" role="alert">{errorMessage}</p>
    {/if}
  </Popover.Content>
</Popover.Root>
