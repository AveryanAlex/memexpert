<script lang="ts">
  import { onDestroy, tick } from 'svelte';
  import { Button, FormRow, Input } from '$lib/ui';

  let formElement: HTMLFormElement | undefined;
  let platformId = $state('');
  let title = $state('');
  let username = $state('');
  let focusRequest = 0;

  onDestroy(() => {
    focusRequest += 1;
  });

  export function prefillAndFocus(reference: string): void {
    platformId = reference;
    if (!title) title = 'Telegram source';
    const request = ++focusRequest;
    void focusPlatformIdAfterUpdate(request);
  }

  async function focusPlatformIdAfterUpdate(request: number): Promise<void> {
    await tick();
    if (request !== focusRequest) return;
    formElement?.querySelector<HTMLInputElement>('[name="platform_id"]')?.focus();
  }
</script>

<form bind:this={formElement} method="POST" action="?/addSourceChannel" class="grid gap-3">
  <input type="hidden" name="platform" value="telegram" />
  <p class="m-0 text-sm text-muted">
    Use this only when you already know the canonical Telegram identifier. New sources are added without an account and with ingestion off. Assign a ready account, then set ingestion options from its source card.
  </p>
  <div class="grid gap-3 md:grid-cols-2">
    <FormRow label="Telegram platform ID"><Input name="platform_id" bind:value={platformId} placeholder="-1001234567890" required /></FormRow>
    <FormRow label="Source title"><Input name="title" bind:value={title} placeholder="Source title" required /></FormRow>
  </div>
  <FormRow label="Handle (optional)"><Input name="username" bind:value={username} placeholder="public_handle" /></FormRow>
  <Button type="submit">Add Telegram source</Button>
</form>
