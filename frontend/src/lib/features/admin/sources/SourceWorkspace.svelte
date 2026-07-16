<script lang="ts">
  import { invalidate } from '$app/navigation';
  import { onDestroy, tick, untrack } from 'svelte';
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { EmptyState, Notice } from '$lib/ui';
  import type { AdminSourceChannelRead, AdminTelegramSessionRead, ChannelSuggestionRead } from '$lib/api/types';
  import AddSourceByReference from './AddSourceByReference.svelte';
  import ManualSourceForm from './ManualSourceForm.svelte';
  import SourceInventoryTable from './SourceInventoryTable.svelte';
  import SourceSuggestionCard from './SourceSuggestionCard.svelte';
  import { isSourceAtLeastExpected, mergeSourceProjections } from './view-model';

  interface SourceWorkspaceData {
    suggestions: ChannelSuggestionRead[];
    sourceChannels: AdminSourceChannelRead[];
    telegramAccounts: AdminTelegramSessionRead[];
  }

  interface SourceWorkspaceErrors {
    suggestions: string | null;
    sourceChannels: string | null;
    telegramAccounts: string | null;
  }

  const NO_SOURCE_WORKSPACE_ERRORS: SourceWorkspaceErrors = {
    suggestions: null,
    sourceChannels: null,
    telegramAccounts: null
  };

  const SOURCE_REFRESH_DELAYS_MS = [0, 1_000, 2_000, 4_000] as const;

  let {
    sourceAdmin,
    sourceAdminErrors = NO_SOURCE_WORKSPACE_ERRORS,
    loadError,
    form
  }: {
    sourceAdmin: SourceWorkspaceData;
    sourceAdminErrors?: SourceWorkspaceErrors;
    loadError: string | null;
    form?: { message?: string; error?: boolean } | null;
  } = $props();

  let referenceSourceForm = $state<{ prefillAndFocus: (reference: string, suggestionId?: string) => void } | null>(null);
  let retainedSuggestions = $state(untrack(() => sourceAdmin.suggestions));
  let retainedSourceChannels = $state(untrack(() => sourceAdmin.sourceChannels));
  let retainedTelegramAccounts = $state(untrack(() => sourceAdmin.telegramAccounts));
  let optimisticSourceChannels = $state<AdminSourceChannelRead[]>([]);
  let approvedSuggestionIds = $state<string[]>([]);
  let refreshRequest = 0;
  let destroyed = false;

  const visibleSuggestions = $derived(
    retainedSuggestions.filter((suggestion) => !approvedSuggestionIds.includes(suggestion.id))
  );
  const visibleSourceChannels = $derived(mergeSourceProjections(retainedSourceChannels, optimisticSourceChannels));
  const visibleSourceAdmin = $derived({
    suggestions: visibleSuggestions,
    sourceChannels: visibleSourceChannels,
    telegramAccounts: retainedTelegramAccounts
  });
  const pendingSuggestions = $derived(visibleSuggestions.filter((suggestion) => suggestion.status === 'pending'));

  $effect(() => {
    if (sourceAdminErrors.suggestions !== null) return;
    retainedSuggestions = sourceAdmin.suggestions;
    const pendingIds = new Set(
      sourceAdmin.suggestions
        .filter((suggestion) => suggestion.status === 'pending')
        .map((suggestion) => suggestion.id)
    );
    const stillPending = approvedSuggestionIds.filter((id) => pendingIds.has(id));
    if (stillPending.length !== approvedSuggestionIds.length) approvedSuggestionIds = stillPending;
  });

  $effect(() => {
    if (sourceAdminErrors.sourceChannels !== null) return;
    retainedSourceChannels = sourceAdmin.sourceChannels;
    const loadedById = new Map(sourceAdmin.sourceChannels.map((source) => [source.id, source]));
    const unresolved = optimisticSourceChannels.filter(
      (source) => !isSourceAtLeastExpected(loadedById.get(source.id), source)
    );
    if (unresolved.length !== optimisticSourceChannels.length) optimisticSourceChannels = unresolved;
  });

  $effect(() => {
    if (sourceAdminErrors.telegramAccounts === null) retainedTelegramAccounts = sourceAdmin.telegramAccounts;
  });

  onDestroy(() => {
    destroyed = true;
    refreshRequest += 1;
  });

  function addSuggestedTelegramSource(suggestion: ChannelSuggestionRead): void {
    referenceSourceForm?.prefillAndFocus(suggestion.channel_url, suggestion.id);
  }

  function sourceAdded(source: AdminSourceChannelRead, suggestionId: string | null): void {
    if (destroyed) return;
    optimisticSourceChannels = [
      ...optimisticSourceChannels.filter((candidate) => candidate.id !== source.id),
      source
    ];
    if (suggestionId && !approvedSuggestionIds.includes(suggestionId)) {
      approvedSuggestionIds = [...approvedSuggestionIds, suggestionId];
    }
    void refreshSourceData(source, suggestionId);
  }

  async function refreshSourceData(
    expectedSource: AdminSourceChannelRead,
    approvedSuggestionId: string | null
  ): Promise<void> {
    const request = ++refreshRequest;
    for (const delayMs of SOURCE_REFRESH_DELAYS_MS) {
      if (delayMs > 0) await new Promise((resolve) => setTimeout(resolve, delayMs));
      if (request !== refreshRequest) return;
      try {
        await invalidate('app:admin-sources');
      } catch {
        // The frontend or API may still be restarting after the write committed.
      }
      await tick();
      if (request !== refreshRequest) return;
      const sourceIsCurrent = sourceAdminErrors.sourceChannels === null
        && isSourceAtLeastExpected(
          sourceAdmin.sourceChannels.find((source) => source.id === expectedSource.id),
          expectedSource
        );
      const accountsAreCurrent = sourceAdminErrors.telegramAccounts === null;
      const suggestionIsCurrent = approvedSuggestionId === null
        || (
          sourceAdminErrors.suggestions === null
          && !sourceAdmin.suggestions.some(
            (suggestion) => suggestion.id === approvedSuggestionId && suggestion.status === 'pending'
          )
        );
      if (sourceIsCurrent && accountsAreCurrent && suggestionIsCurrent) return;
    }
  }
</script>

<section class="grid gap-3">
  <p class="m-0 text-sm font-black uppercase tracking-[0.16em] text-muted">Source management</p>
  <h1 class="m-0 text-[clamp(2.4rem,8vw,5rem)] font-black leading-[0.9] tracking-[-0.075em]">Sources</h1>
  <p class="m-0 max-w-3xl text-muted">Review suggestions, keep active sources healthy, and assign the Telegram account that should fetch each source.</p>
</section>

{#if form?.message}
  <Notice tone={form.error ? 'danger' : 'success'} role={form.error ? 'alert' : 'status'}>{form.message}</Notice>
{/if}

{#if loadError}
  <Notice tone="danger" role="alert">{loadError}</Notice>
{/if}

<AddSourceByReference bind:this={referenceSourceForm} telegramAccounts={visibleSourceAdmin.telegramAccounts} onSourceAdded={sourceAdded} />

<AdminPanel title="Suggested sources">
  {#if pendingSuggestions.length === 0}
    <EmptyState title="No pending suggestions" message="New source suggestions will appear here for review." />
  {:else}
    <div class="grid gap-4 lg:grid-cols-2">
      {#each pendingSuggestions as suggestion (suggestion.id)}
        <SourceSuggestionCard {suggestion} onAddTelegram={addSuggestedTelegramSource} />
      {/each}
    </div>
  {/if}
</AdminPanel>

<AdvancedSection title="Advanced manual source entry" description="Use the canonical Telegram identifier when a suggestion cannot wait for reference-based setup.">
  <ManualSourceForm />
</AdvancedSection>

<AdminPanel title="All sources">
  {#if visibleSourceAdmin.sourceChannels.length === 0}
    <EmptyState title="No sources yet" message="Review a suggestion or use the advanced Telegram entry when you know the canonical identifier." />
  {:else}
    <SourceInventoryTable sources={visibleSourceAdmin.sourceChannels} telegramAccounts={visibleSourceAdmin.telegramAccounts} />
  {/if}
</AdminPanel>
