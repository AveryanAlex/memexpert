<script lang="ts">
  import { onDestroy, tick, untrack } from 'svelte';
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import type { AdminTelegramSessionRead } from '$lib/api/types';
  import { Button, FormRow, Input, Notice } from '$lib/ui';
  import { clearSourceSuggestionPrefill, readyTelegramAccounts, sourceSuggestionPrefill } from './view-model';

  let {
    telegramAccounts,
    initialReference = '',
    initialSuggestionId = ''
  }: {
    telegramAccounts: AdminTelegramSessionRead[];
    initialReference?: string;
    initialSuggestionId?: string;
  } = $props();

  const readyAccounts = $derived(readyTelegramAccounts(telegramAccounts));
  const initialPrefill = untrack(() => sourceSuggestionPrefill(initialReference, initialSuggestionId));
  let reference = $state(initialPrefill.reference);
  let suggestionId = $state(initialPrefill.suggestionId);
  let formElement: HTMLFormElement | undefined;
  let focusRequest = 0;

  onDestroy(() => {
    focusRequest += 1;
  });

  export function prefillAndFocus(nextReference: string, nextSuggestionId = ''): void {
    const prefill = sourceSuggestionPrefill(nextReference, nextSuggestionId);
    reference = prefill.reference;
    suggestionId = prefill.suggestionId;
    requestReferenceFocus();
  }

  function clearSuggestion(): void {
    const cleared = clearSourceSuggestionPrefill();
    reference = cleared.reference;
    suggestionId = cleared.suggestionId;
    requestReferenceFocus();
  }

  function requestReferenceFocus(): void {
    const request = ++focusRequest;
    void focusReferenceAfterUpdate(request);
  }

  async function focusReferenceAfterUpdate(request: number): Promise<void> {
    await tick();
    if (request !== focusRequest) return;
    formElement?.querySelector<HTMLInputElement>('[name="reference"]')?.focus();
  }
</script>

<AdminPanel title="Add Telegram source">
  <form bind:this={formElement} method="POST" action="?/addSourceByReference" class="grid gap-4">
    <p class="m-0 text-sm text-muted">Paste one public channel link or @handle. MemeExpert resolves the channel through the selected ready account and enables safe ingestion defaults.</p>
    <input type="hidden" name="suggestion_id" value={suggestionId} />
    <div class="grid gap-3 md:grid-cols-2">
      <FormRow label="Channel link or @handle">
        <Input name="reference" bind:value={reference} placeholder="@public_channel" required />
      </FormRow>
      <FormRow label="Telegram account">
        <select
          name="telegram_session_id"
          required
          class="min-h-11 w-full rounded-2xl border border-line bg-paper px-3 py-2 text-sm text-ink"
        >
          <option value="" disabled>Choose a ready account</option>
          {#each readyAccounts as account (account.id)}
            <option value={account.id} selected={readyAccounts.length === 1}>{account.display_name}</option>
          {/each}
        </select>
      </FormRow>
    </div>

    {#if suggestionId}
      <Notice>
        <span class="grid gap-2">
          <span><strong>Selected suggestion:</strong> {reference}</span>
          <span>This source and its matching suggestion will be saved together.</span>
          <Button type="button" variant="secondary" onclick={clearSuggestion}>Cancel suggestion</Button>
        </span>
      </Notice>
    {/if}
    {#if readyAccounts.length === 0}
      <Notice>No Telegram account is ready. <a class="font-black underline" href="/admin/telegram">Connect or repair an account</a> first.</Notice>
    {:else if readyAccounts.length > 1}
      <p class="m-0 text-sm text-muted">Choose which ready account should fetch this source.</p>
    {/if}

    <AdvancedSection title="Advanced settings" description="Change the bounded first catch-up only when this source needs an exception.">
      <FormRow label="Catch-up message limit">
        <Input name="catchup_message_limit" type="number" min="1" max="10000" value="5000" required />
      </FormRow>
    </AdvancedSection>

    <Button type="submit" disabled={readyAccounts.length === 0}>Add source</Button>
  </form>
</AdminPanel>
