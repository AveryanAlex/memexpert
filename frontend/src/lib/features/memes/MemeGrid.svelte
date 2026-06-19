<script lang="ts">
  import { browser } from '$app/environment';
  import { invalidateAll } from '$app/navigation';
  import { recordMemeDownload } from '$lib/api/client';
  import type { MemeResultAttributionRead, PublicMemeCardRead } from '$lib/api/types';
  import { memeActionAttributionBody } from '$lib/memeActions';
  import { Button, Select } from '$lib/ui';
  import { Download } from '@lucide/svelte';
  import {
    bulkDownloadItems,
    bulkToolbarSummary,
    selectedMemes,
    type MemeGridBulkOptions
  } from './bulk-view-model';
  import MemeCard from './MemeCard.svelte';
  import { buildMasonryColumns, masonryColumnCount, masonryColumnWidth } from './masonry-layout';

  let {
    memes,
    label = 'Meme results',
    attributions = {},
    bulk = { enabled: false }
  }: {
    memes: PublicMemeCardRead[];
    label?: string;
    attributions?: Record<string, MemeResultAttributionRead | null | undefined>;
    bulk?: MemeGridBulkOptions;
  } = $props();

  let selectedIds = $state<string[]>([]);
  let targetCollectionId = $state('');
  let pendingAction = $state<string | null>(null);
  let statusMessage = $state<string | null>(null);
  let gridElement = $state<HTMLElement>();
  let gridWidth = $state(0);

  const columnCount = $derived(masonryColumnCount(gridWidth));
  const columnWidth = $derived(masonryColumnWidth(gridWidth, columnCount));
  // Preserve backend ranking by processing memes strictly in array order. Each meme is appended to
  // the current shortest estimated column, so the layout is deterministic without random shuffling.
  const masonryColumns = $derived(buildMasonryColumns(memes, columnCount, columnWidth));

  const bulkEnabled = $derived(Boolean(bulk.enabled));
  const selected = $derived(selectedMemes(memes, selectedIds));
  const downloadable = $derived(bulkDownloadItems(selected));
  const allSelected = $derived(memes.length > 0 && selectedIds.length === memes.length);
  const collectionOptions = $derived(bulk.collectionOptions ?? []);
  const canAddToCollection = $derived(collectionOptions.length > 0);
  const canRemoveFromCollection = $derived(Boolean(bulk.removeEnabled && bulk.removeCollectionId));
  const toolbarSummary = $derived(bulkToolbarSummary(memes.length, selected.length, downloadable.length));
  const memePositions = $derived(new Map(memes.map((meme, index) => [meme.id, index + 1])));

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
    if (!browser || !gridElement) return;

    const updateGridWidth = () => {
      gridWidth = gridElement?.clientWidth ?? 0;
    };
    const observer = new ResizeObserver(updateGridWidth);
    updateGridWidth();
    observer.observe(gridElement);

    return () => observer.disconnect();
  });

  function toggleSelection(memeId: string) {
    statusMessage = null;
    selectedIds = selectedIds.includes(memeId) ? selectedIds.filter((id) => id !== memeId) : [...selectedIds, memeId];
  }

  function toggleAll() {
    statusMessage = null;
    selectedIds = allSelected ? [] : memes.map((meme) => meme.id);
  }

  function clearSelection() {
    selectedIds = [];
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
        throw new Error(`Could not update ${meme.caption || meme.tags[0] || 'selected meme'}: ${detail}`);
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

{#if bulkEnabled}
  <div class="mb-4 grid gap-3 rounded-[28px] border border-line bg-paper p-4 shadow-warm" aria-label={`${label} bulk actions`}>
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p class="m-0 font-black">Bulk actions</p>
        <p class="m-0 text-sm text-muted">{toolbarSummary}</p>
      </div>
      <div class="flex flex-wrap gap-2">
        <Button size="compact" variant="secondary" type="button" onclick={toggleAll} disabled={memes.length === 0 || pendingAction !== null}>{allSelected ? 'Clear all' : 'Select all'}</Button>
        <Button size="compact" variant="ghost" type="button" onclick={clearSelection} disabled={selected.length === 0 || pendingAction !== null}>Clear</Button>
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
    {#if statusMessage}
      <p class="m-0 text-sm text-muted" role="status">{statusMessage}</p>
    {/if}
  </div>
{/if}

<section bind:this={gridElement} class="flex gap-4" aria-label={label} data-column-count={columnCount} role="list" aria-busy={pendingAction !== null}>
  {#each masonryColumns as column (column.id)}
    <div class="grid min-w-0 flex-1 content-start gap-4" role="presentation">
      {#each column.items as meme (meme.id)}
        {@const attribution = attributions[meme.id]}
        <div
          class="relative"
          role="presentation"
          data-discovery-source={attribution?.source_algorithm ?? undefined}
          data-discovery-reason={attribution?.reason ?? undefined}
          data-discovery-request-id={attribution?.request_id ?? undefined}
          data-discovery-impression-id={attribution?.impression_id ?? undefined}
          data-discovery-source-meme-id={attribution?.source_meme_id ?? undefined}
          data-discovery-score={attribution?.score ?? undefined}
        >
          {#if bulkEnabled}
            <label class="absolute left-3 top-3 z-20 inline-flex items-center gap-2 rounded-full border border-line bg-paper/95 px-3 py-2 text-sm font-extrabold shadow-warm">
              <input type="checkbox" checked={selectedIds.includes(meme.id)} onchange={() => toggleSelection(meme.id)} aria-label={`Select ${meme.caption || meme.tags[0] || 'meme'}`} />
              Select
            </label>
          {/if}
          <MemeCard {meme} {attribution} position={memePositions.get(meme.id)} total={memes.length} />
        </div>
      {/each}
    </div>
  {/each}
</section>
