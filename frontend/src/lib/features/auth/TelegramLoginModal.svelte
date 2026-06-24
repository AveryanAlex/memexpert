<script lang="ts">
  import { browser } from '$app/environment';
  import { invalidateAll } from '$app/navigation';
  import { Mail, Send, Sparkles } from '@lucide/svelte';
  import type { CurrentSessionRead, TelegramLinkStartRead } from '$lib/api/types';
  import { Button, LoadingState, Notice } from '$lib/ui';
  import * as Dialog from '$lib/ui/dialog';
  import { buildTelegramStartCommand, isFullSession, LOGIN_PROVIDER_OPTIONS, TELEGRAM_LOGIN_POLL_INTERVAL_MS, telegramExpiryLabel } from './telegram-login';

  let { open = $bindable(false), session = null }: { open?: boolean; session: CurrentSessionRead | null } = $props();

  let link = $state<TelegramLinkStartRead | null>(null);
  let starting = $state(false);
  let polling = $state(false);
  let errorMessage = $state<string | null>(null);
  let copyMessage = $state<string | null>(null);
  let completed = $state(false);
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  const command = $derived(link ? buildTelegramStartCommand(link) : '');

  $effect(() => {
    if (!open) {
      stopPolling();
      return;
    }
    if (link && !polling && !completed) startPolling();
  });

  async function startTelegram() {
    starting = true;
    errorMessage = null;
    copyMessage = null;
    try {
      const response = await fetch('/api/v1/auth/link/telegram', {
        method: 'POST',
        credentials: 'include',
        headers: { accept: 'application/json', 'x-requested-with': 'XMLHttpRequest' }
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error(typeof payload?.detail === 'string' ? payload.detail : 'Could not start Telegram login.');
      link = payload as TelegramLinkStartRead;
      startPolling();
    } catch (error) {
      errorMessage = error instanceof Error ? error.message : 'Could not start Telegram login.';
    } finally {
      starting = false;
    }
  }

  async function copyCommand() {
    if (!command || !browser) return;
    try {
      await navigator.clipboard.writeText(command);
      copyMessage = 'Command copied. Paste it into the MemeXpert bot chat.';
    } catch {
      errorMessage = 'Could not copy automatically. Select the command and copy it manually.';
    }
  }

  function startPolling() {
    stopPolling();
    polling = true;
    void pollSession();
    pollTimer = setInterval(() => void pollSession(), TELEGRAM_LOGIN_POLL_INTERVAL_MS);
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
    polling = false;
  }

  async function pollSession() {
    try {
      const response = await fetch('/api/v1/auth/session', { credentials: 'include', headers: { accept: 'application/json' } });
      if (!response.ok) return;
      const nextSession = (await response.json()) as CurrentSessionRead;
      if (isFullSession(nextSession)) {
        completed = true;
        stopPolling();
        await invalidateAll();
        open = false;
      }
    } catch {
      // Keep polling; transient network failures should not make users restart the login flow.
    }
  }
</script>

<Dialog.Root bind:open>
  <Dialog.Content class="max-w-[520px] border-white/10 bg-slate-950 text-white shadow-[0_32px_90px_rgb(2_6_23_/_55%)]" aria-labelledby="login-title" aria-describedby="login-description">
    <Dialog.Title id="login-title" class="m-0 text-3xl font-black tracking-[-0.05em]">Sign in to MemeXpert</Dialog.Title>
    <Dialog.Description id="login-description" class="m-0 text-sm text-slate-300">Keep favorites, saves, collections, and recommendations across devices.</Dialog.Description>

    {#if link}
      <section class="grid gap-4 rounded-[28px] border border-sky-400/25 bg-sky-400/10 p-4" aria-label="Telegram login waiting state">
        <div class="flex items-center gap-3">
          <span class="grid size-12 place-items-center rounded-full bg-[#229ED9] text-white"><Send class="size-5" aria-hidden="true" /></span>
          <div>
            <p class="m-0 font-black">Waiting for Telegram confirmation</p>
            <p class="m-0 text-sm text-slate-300">{telegramExpiryLabel(link)}</p>
          </div>
        </div>
        <a class="inline-flex items-center justify-center gap-2 rounded-2xl bg-[#229ED9] px-5 py-3 font-black text-white no-underline hover:bg-[#1d8fc5]" href={link.deep_link_url} target="_blank" rel="noreferrer"><Send class="size-4" aria-hidden="true" /> Open Telegram</a>
        <div class="rounded-2xl border border-white/10 bg-black/30 p-3">
          <p class="m-0 text-xs uppercase tracking-[0.18em] text-slate-400">Manual command</p>
          <code class="mt-1 block break-all text-sm text-slate-100">{command}</code>
          <Button class="mt-3 bg-white text-slate-950 hover:bg-slate-200" type="button" size="compact" onclick={copyCommand}>Copy command</Button>
        </div>
        {#if polling}<LoadingState label="Checking login status every second" />{/if}
        {#if copyMessage}<p class="m-0 text-sm text-slate-300" role="status">{copyMessage}</p>{/if}
        {#if completed}<Notice>Telegram connected. Refreshing your session…</Notice>{/if}
      </section>
    {:else}
      <div class="grid gap-3">
        <button class="flex w-full items-center gap-3 rounded-[24px] bg-[#229ED9] p-4 text-left font-black text-white transition hover:bg-[#1d8fc5] disabled:opacity-70" type="button" onclick={startTelegram} disabled={starting}>
          <span class="grid size-11 place-items-center rounded-full bg-white/15"><Send class="size-5" aria-hidden="true" /></span>
          <span class="grid gap-0.5"><span>Continue with {LOGIN_PROVIDER_OPTIONS[0].label}</span><span class="text-sm font-medium text-white/80">{LOGIN_PROVIDER_OPTIONS[0].description}</span></span>
        </button>
        <button class="flex w-full cursor-not-allowed items-center gap-3 rounded-[24px] border border-white/10 bg-white/5 p-4 text-left text-slate-400" type="button" disabled><Sparkles class="size-5" aria-hidden="true" /> {LOGIN_PROVIDER_OPTIONS[1].label} <span class="ml-auto rounded-full bg-white/10 px-3 py-1 text-xs">Coming later</span></button>
        <button class="flex w-full cursor-not-allowed items-center gap-3 rounded-[24px] border border-white/10 bg-white/5 p-4 text-left text-slate-400" type="button" disabled><Mail class="size-5" aria-hidden="true" /> {LOGIN_PROVIDER_OPTIONS[2].label} <span class="ml-auto rounded-full bg-white/10 px-3 py-1 text-xs">Coming later</span></button>
        {#if starting}<LoadingState label="Starting Telegram login" />{/if}
      </div>
    {/if}

    {#if errorMessage}<Notice tone="danger" role="alert">{errorMessage}</Notice>{/if}
  </Dialog.Content>
</Dialog.Root>
