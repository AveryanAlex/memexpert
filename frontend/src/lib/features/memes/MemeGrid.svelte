<script lang="ts">
  import { browser } from '$app/environment';
  import { invalidateAll } from '$app/navigation';
  import { recordMemeDownload } from '$lib/api/client';
  import type { MemeResultAttributionRead, PublicMemeCardRead } from '$lib/api/types';
  import { memeActionAttributionBody, memeTitle } from '$lib/memeActions';
  import { Button, Masonry, Select } from '$lib/ui';
  import { Download } from '@lucide/svelte';
  import {
    bulkDownloadItems,
    bulkToolbarSummary,
    selectedMemes,
    type MemeGridBulkOptions
  } from './bulk-view-model';
  import { memeDiscoveryDataAttributes } from './discovery-attribution';
  import MemeCard from './MemeCard.svelte';
  import type { MemeVideoPreviewMode } from './meme-video';

  let {
    memes,
    label = 'Meme results',
    attributions = {},
    bulk = { enabled: false },
    showAccessMarkers = false,
    exposureScope
  }: {
    memes: PublicMemeCardRead[];
    label?: string;
    attributions?: Record<string, MemeResultAttributionRead | null | undefined>;
    bulk?: MemeGridBulkOptions;
    showAccessMarkers?: boolean;
    exposureScope?: string;
  } = $props();

  let selectedIds = $state<string[]>([]);
  let selectionMode = $state(false);
  let targetCollectionId = $state('');
  let pendingAction = $state<string | null>(null);
  let statusMessage = $state<string | null>(null);
  let renderedColumnCount = $state(1);
  let masonryReady = $state(false);
  let hydrated = $state(false);

  const bulkEnabled = $derived(Boolean(bulk.enabled));
  const selected = $derived(selectedMemes(memes, selectedIds));
  const downloadable = $derived(bulkDownloadItems(selected));
  const allSelected = $derived(memes.length > 0 && selectedIds.length === memes.length);
  const collectionOptions = $derived(bulk.collectionOptions ?? []);
  const canAddToCollection = $derived(collectionOptions.length > 0);
  const canRemoveFromCollection = $derived(Boolean(bulk.removeEnabled && bulk.removeCollectionId));
  const toolbarSummary = $derived(bulkToolbarSummary(memes.length, selected.length, downloadable.length));
  const memePositions = $derived(new Map(memes.map((meme, index) => [meme.id, index + 1])));
  const exposureScopeKey = $derived(exposureScope?.trim() || `grid:${label}:masonry`);
  const videoPreviewMode = $derived<MemeVideoPreviewMode>(
    !hydrated || !masonryReady ? 'poster' : renderedColumnCount === 1 ? 'viewport' : 'hover'
  );

  $effect(() => {
    const availableIds = new Set(memes.map((meme) => meme.id));
    const nextSelectedIds = selectedIds.filter((id) => availableIds.has(id));
    if (nextSelectedIds.length !== selectedIds.length) {
      selectedIds = nextSelectedIds;
    }

    if (collectionOptions.length > 0 && !collectionOptions.some((collection) => collection.id === targetCollectionId)) {
      targetCollectionId = collectionOptions[0].id;
    }
  });

  $effect(() => {
    if (!bulkEnabled && (selectionMode || selectedIds.length > 0)) {
      selectionMode = false;
      selectedIds = [];
    }
  });

  $effect(() => {
    hydrated = true;
  });

  function toggleSelection(memeId: string) {
    statusMessage = null;
    selectedIds = selectedIds.includes(memeId) ? selectedIds.filter((id) => id !== memeId) : [...selectedIds, memeId];
  }

  function exposureIdFor(attribution: MemeResultAttributionRead | null | undefined): string | undefined {
    return attribution?.impression_id;
  }

  function memeKey(meme: PublicMemeCardRead): string {
    return meme.id;
  }

  function startSelection() {
    selectionMode = true;
    statusMessage = null;
  }

  function toggleAll() {
    statusMessage = null;
    selectedIds = allSelected ? [] : memes.map((meme) => meme.id);
  }

  function clearSelection() {
    selectedIds = [];
    selectionMode = false;
    statusMessage = null;
  }

  function finishSelection() {
    selectedIds = [];
    selectionMode = false;
    statusMessage = null;
  }

  async function saveSelectedToActive() {
    await runBulkAction('save', 'Saving selected memes...', async () => {
      await mutateSelected(
        (meme) => `/api/v1/memes/${encodeURIComponent(meme.id)}/save`,
        'POST',
        (meme) => memeActionAttributionBody(attributions[meme.id])
      );
      statusMessage = `${selected.length} selected meme${selected.length === 1 ? '' : 's'} saved to the active collection.`;
    });
  }

  async function addSelectedToCollection() {
    if (!targetCollectionId) return;

    const targetTitle = collectionOptions.find((collection) => collection.id === targetCollectionId)?.title ?? 'collection';
    await runBulkAction('add', `Adding selected memes to ${targetTitle}...`, async () => {
      await mutateSelected(
        (meme) => `/api/v1/collections/${encodeURIComponent(targetCollectionId)}/memes/${encodeURIComponent(meme.id)}`,
        'POST'
      );
      statusMessage = `${selected.length} selected meme${selected.length === 1 ? '' : 's'} added to ${targetTitle}.`;
    });
  }

  async function removeSelectedFromCollection() {
    const collectionId = bulk.removeCollectionId;
    if (!collectionId) return;

    await runBulkAction('remove', 'Removing selected memes...', async () => {
      await mutateSelected(
        (meme) => `/api/v1/collections/${encodeURIComponent(collectionId)}/memes/${encodeURIComponent(meme.id)}`,
        'DELETE'
      );
      statusMessage = `${selected.length} selected meme${selected.length === 1 ? '' : 's'} removed from this collection.`;
      selectedIds = [];
      selectionMode = false;
    });
  }

  function downloadSelected() {
    if (!browser || downloadable.length === 0) {
      statusMessage = 'No selected memes have a media URL for download.';
      return;
    }

    for (const item of downloadable) {
      recordBulkDownloadTelemetry(item.id);
      const link = document.createElement('a');
      link.href = item.url;
      link.download = downloadName(item.title);
      link.rel = 'noopener noreferrer';
      document.body.append(link);
      link.click();
      link.remove();
    }

    statusMessage = `Started ${downloadable.length} download${downloadable.length === 1 ? '' : 's'}.`;
  }

  async function runBulkAction(action: string, pendingText: string, callback: () => Promise<void>) {
    if (pendingAction || selected.length === 0) return;
    pendingAction = action;
    statusMessage = pendingText;
    try {
      await callback();
      await invalidateAll();
    } catch (error) {
      statusMessage = error instanceof Error ? error.message : 'Bulk action failed.';
    } finally {
      pendingAction = null;
    }
  }

  async function mutateSelected(
    urlForMeme: (meme: PublicMemeCardRead) => string,
    method: 'DELETE' | 'POST',
    bodyForMeme?: (meme: PublicMemeCardRead) => unknown | undefined
  ) {
    for (const meme of selected) {
      const body = bodyForMeme?.(meme);
      const headers = new Headers({ accept: 'application/json', 'x-requested-with': 'XMLHttpRequest' });
      if (body !== undefined) {
        headers.set('content-type', 'application/json');
      }
      const response = await fetch(urlForMeme(meme), {
        method,
        credentials: 'include',
        headers,
        body: body === undefined ? undefined : JSON.stringify(body)
      });

      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        const detail = typeof payload?.detail === 'string' ? payload.detail : `HTTP ${response.status}`;
        throw new Error(`Could not update ${memeTitle(meme)}: ${detail}`);
      }
    }
  }

  function recordBulkDownloadTelemetry(memeId: string) {
    void recordMemeDownload({
      fetch,
      memeId,
      body: memeActionAttributionBody(attributions[memeId]),
      keepalive: true
    }).catch((error: unknown) => console.warn('Bulk meme download telemetry failed.', { memeId, error }));
  }

  function downloadName(title: string): string {
    return title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'meme';
  }
</script>

{#if bulkEnabled && !selectionMode}
  <div class="mb-3 flex justify-end">
    <Button size="compact" variant="ghost" type="button" onclick={startSelection} disabled={!hydrated || memes.length === 0}>Select items</Button>
  </div>
{:else if bulkEnabled && selectionMode}
  <div class="sticky top-16 z-20 mb-4 grid gap-3 rounded-xl border border-line bg-paper/95 p-3 shadow-overlay backdrop-blur" aria-label={`${label} selection actions`}>
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p class="m-0 font-semibold">Select items</p>
        <p class="m-0 text-sm text-muted">{toolbarSummary}</p>
      </div>
      <div class="flex flex-wrap gap-2">
        <Button size="compact" variant="secondary" type="button" onclick={toggleAll} disabled={memes.length === 0 || pendingAction !== null}>{allSelected ? 'Clear all' : 'Select all'}</Button>
        <Button size="compact" variant="ghost" type="button" onclick={clearSelection} disabled={pendingAction !== null}>Clear</Button>
        <Button size="compact" variant="ghost" type="button" onclick={finishSelection} disabled={pendingAction !== null}>Done</Button>
      </div>
    </div>

    <div class="flex flex-wrap items-center gap-2">
      {#if bulk.saveEnabled}
        <Button size="compact" type="button" onclick={saveSelectedToActive} disabled={selected.length === 0 || pendingAction !== null}>Save selected</Button>
      {/if}

      {#if canAddToCollection}
        <Select class="max-w-[260px]" bind:value={targetCollectionId} aria-label="Bulk add collection" disabled={pendingAction !== null}>
          {#each collectionOptions as collection (collection.id)}
            <option value={collection.id}>{collection.title}</option>
          {/each}
        </Select>
        <Button size="compact" variant="secondary" type="button" onclick={addSelectedToCollection} disabled={selected.length === 0 || pendingAction !== null || !targetCollectionId}>Add to collection</Button>
      {/if}

      {#if canRemoveFromCollection}
        <Button size="compact" variant="danger" type="button" onclick={removeSelectedFromCollection} disabled={selected.length === 0 || pendingAction !== null}>Remove selected</Button>
      {/if}

      <Button size="compact" variant="secondary" type="button" onclick={downloadSelected} disabled={downloadable.length === 0 || pendingAction !== null}>
        <Download class="size-4" aria-hidden="true" />
        Download selected
      </Button>
    </div>

    {#if bulk.guidance}
      <p class="m-0 text-sm text-muted">{bulk.guidance}</p>
    {/if}
  </div>
{/if}

{#if statusMessage}
  <p class="mb-3 text-sm text-muted" role="status">{statusMessage}</p>
{/if}

<Masonry
  bind:columnCount={renderedColumnCount}
  bind:ready={masonryReady}
  items={memes}
  getKey={memeKey}
  element="section"
  aria-label={label}
  data-video-preview-mode={videoPreviewMode}
  role="list"
  busy={pendingAction !== null}
>
  {#snippet children(meme)}
    {@const attribution = attributions[meme.id]}
    {@const discoveryAttributes = memeDiscoveryDataAttributes(attribution)}
    <div
      class="relative min-w-0 max-w-full"
      role="presentation"
      {...discoveryAttributes}
    >
      {#if selectionMode}
        <label class="absolute left-3 top-3 z-20 inline-flex items-center gap-2 rounded-full border border-line bg-paper/95 px-3 py-2 text-sm font-extrabold shadow-warm">
          <input type="checkbox" checked={selectedIds.includes(meme.id)} onchange={() => toggleSelection(meme.id)} aria-label={`Select ${memeTitle(meme)}`} />
          Select
        </label>
      {/if}
      <MemeCard
        {meme}
        {attribution}
        exposureId={exposureIdFor(attribution)}
        exposurePlacement={`${exposureScopeKey}:${memePositions.get(meme.id) ?? 0}:${meme.id}`}
        position={memePositions.get(meme.id)}
        total={memes.length}
        {showAccessMarkers}
        showZoom={masonryReady && renderedColumnCount > 1}
        {videoPreviewMode}
      />
    </div>
  {/snippet}
</Masonry>
