<script lang="ts">
  import { invalidateAll } from '$app/navigation';
  import { onDestroy, tick, untrack } from 'svelte';
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { ApiError } from '$lib/api/client';
  import type { AdminTelegramChannelFromReferencePayload, AdminTelegramSessionRead } from '$lib/api/types';
  import { Button, FormRow, Input, Notice } from '$lib/ui';
  import { addTelegramSourceWithRetry } from './add-source-client';
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
  let submitting = $state(false);
  let retrying = $state(false);
  let feedback = $state<{ message: string; tone: 'neutral' | 'danger' | 'success' } | null>(null);

  onDestroy(() => {
    focusRequest += 1;
  });

  export function prefillAndFocus(nextReference: string, nextSuggestionId = ''): void {
    const prefill = sourceSuggestionPrefill(nextReference, nextSuggestionId);
    reference = prefill.reference;
    suggestionId = prefill.suggestionId;
    feedback = null;
    requestReferenceFocus();
  }

  function clearSuggestion(): void {
    const cleared = clearSourceSuggestionPrefill();
    reference = cleared.reference;
    suggestionId = cleared.suggestionId;
    feedback = null;
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

  async function handleSubmit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!formElement || submitting) return;

    const data = new FormData(formElement);
    const payload = sourcePayload(data);
    const approvesSuggestion = Boolean(payload.suggestion_id);
    submitting = true;
    retrying = false;
    feedback = null;

    try {
      await addTelegramSourceWithRetry({
        fetch,
        baseUrl: window.location.origin,
        body: payload,
        onRetry: () => {
          retrying = true;
          feedback = {
            message: 'The gateway interrupted this request. Retrying automatically…',
            tone: 'neutral'
          };
        }
      });
      formElement.reset();
      reference = '';
      suggestionId = '';
      feedback = {
        message: approvesSuggestion
          ? 'Telegram source added and suggestion approved.'
          : 'Telegram source added and ready to fetch.',
        tone: 'success'
      };
      void refreshSourceData();
    } catch (error) {
      feedback = {
        message: error instanceof ApiError ? error.message : 'Could not add the Telegram source. Check the connection and try again.',
        tone: 'danger'
      };
    } finally {
      submitting = false;
      retrying = false;
    }
  }

  function sourcePayload(data: FormData): AdminTelegramChannelFromReferencePayload {
    const catchupMessageLimit = Number.parseInt(String(data.get('catchup_message_limit') ?? ''), 10);
    return {
      reference: String(data.get('reference') ?? '').trim(),
      telegram_session_id: String(data.get('telegram_session_id') ?? '').trim(),
      suggestion_id: String(data.get('suggestion_id') ?? '').trim() || null,
      catchup_message_limit: Number.isInteger(catchupMessageLimit) ? catchupMessageLimit : 5_000
    };
  }

  async function refreshSourceData(): Promise<void> {
    for (const delayMs of [0, 1_000, 2_000, 4_000]) {
      if (delayMs > 0) await new Promise((resolve) => setTimeout(resolve, delayMs));
      try {
        await invalidateAll();
        return;
      } catch {
        // The frontend may still be restarting after the API accepted the source.
      }
    }
  }
</script>

<AdminPanel title="Add Telegram source">
  <form bind:this={formElement} method="POST" action="?/addSourceByReference" class="grid gap-4" onsubmit={handleSubmit} aria-busy={submitting}>
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

    <Button type="submit" disabled={readyAccounts.length === 0 || submitting}>
      {retrying ? 'Retrying…' : submitting ? 'Adding…' : 'Add source'}
    </Button>
    {#if feedback}
      <Notice class="my-0" tone={feedback.tone} role={feedback.tone === 'danger' ? 'alert' : 'status'}>{feedback.message}</Notice>
    {/if}
  </form>
</AdminPanel>
