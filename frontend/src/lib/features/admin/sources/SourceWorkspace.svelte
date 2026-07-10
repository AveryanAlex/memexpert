<script lang="ts">
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { EmptyState, Notice } from '$lib/ui';
  import type { AdminSourceChannelRead, AdminTelegramSessionRead, ChannelSuggestionRead } from '$lib/api/types';
  import AddSourceByReference from './AddSourceByReference.svelte';
  import ManualSourceForm from './ManualSourceForm.svelte';
  import SourceCard from './SourceCard.svelte';
  import SourceSuggestionCard from './SourceSuggestionCard.svelte';

  interface SourceWorkspaceData {
    suggestions: ChannelSuggestionRead[];
    sourceChannels: AdminSourceChannelRead[];
    telegramAccounts: AdminTelegramSessionRead[];
  }

  let {
    sourceAdmin,
    loadError,
    form
  }: {
    sourceAdmin: SourceWorkspaceData;
    loadError: string | null;
    form?: { message?: string; error?: boolean } | null;
  } = $props();

  let referenceSourceForm = $state<{ prefillAndFocus: (reference: string, suggestionId?: string) => void } | null>(null);
  const pendingSuggestions = $derived(sourceAdmin.suggestions.filter((suggestion) => suggestion.status === 'pending'));

  function addSuggestedTelegramSource(suggestion: ChannelSuggestionRead): void {
    referenceSourceForm?.prefillAndFocus(suggestion.channel_url, suggestion.id);
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

<AddSourceByReference bind:this={referenceSourceForm} telegramAccounts={sourceAdmin.telegramAccounts} />

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
  {#if sourceAdmin.sourceChannels.length === 0}
    <EmptyState title="No sources yet" message="Review a suggestion or use the advanced Telegram entry when you know the canonical identifier." />
  {:else}
    <div class="grid gap-4">
      {#each sourceAdmin.sourceChannels as source (source.id)}
        <SourceCard {source} telegramAccounts={sourceAdmin.telegramAccounts} />
      {/each}
    </div>
  {/if}
</AdminPanel>
