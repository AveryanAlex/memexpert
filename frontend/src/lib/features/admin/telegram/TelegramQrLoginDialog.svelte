<script lang="ts">
  import { browser } from '$app/environment';
  import { invalidateAll } from '$app/navigation';
  import type { AdminTelegramLoginQrStatusRead } from '$lib/api/types';
  import { qrSvgDataUri } from '$lib/qr/svg';
  import { Button, LoadingState, Notice } from '$lib/ui';
  import * as Dialog from '$lib/ui/dialog';
  import { QrRequestLifecycle, type QrRequestToken } from './qr-lifecycle';
  import { qrLoginStep, safeOperatorMessage, type TelegramPasswordLoginStep, type TelegramQrLoginStep } from './view-model';

  let {
    form,
    onPasswordRequired
  }: {
    form: unknown;
    onPasswordRequired: (step: TelegramPasswordLoginStep) => void;
  } = $props();

  let modalOpen = $state(false);
  let sessionId = $state<string | null>(null);
  let attemptId = $state<string | null>(null);
  let qrUrl = $state<string | null>(null);
  let expiresAt = $state<string | null>(null);
  let message = $state('Waiting for scan…');
  let refreshing = $state(false);
  let polling = $state(false);
  let error = $state<string | null>(null);
  let openedAttemptFromForm = $state<string | null>(null);
  let pollAbort: AbortController | null = null;
  let refreshTimer: ReturnType<typeof setTimeout> | null = null;
  const requestLifecycle = new QrRequestLifecycle();

  $effect(() => {
    const step = qrLoginStep(form);
    if (!step || openedAttemptFromForm === step.attemptId) return;
    openedAttemptFromForm = step.attemptId;
    openModal(step);
  });

  $effect(() => {
    if (modalOpen) return;
    stopPolling();
    clearRefreshTimer();
    requestLifecycle.cancel();
  });

  $effect(() => {
    return () => {
      stopPolling();
      clearRefreshTimer();
      requestLifecycle.cancel();
    };
  });

  export async function startExistingQrLogin(event: SubmitEvent): Promise<void> {
    const formElement = event.currentTarget instanceof HTMLFormElement ? event.currentTarget : null;
    if (!formElement) return;
    const nextSessionId = String(new FormData(formElement).get('session_id') ?? '').trim();

    // A new account has no ID yet. Let its named server action create it, then open
    // the QR dialog from the returned form data.
    if (!nextSessionId) return;

    event.preventDefault();
    stopPolling();
    clearRefreshTimer();
    requestLifecycle.cancel();
    sessionId = nextSessionId;
    attemptId = null;
    qrUrl = null;
    expiresAt = null;
    message = 'Loading a new QR code…';
    error = null;
    refreshing = true;
    modalOpen = true;
    const requestToken = requestLifecycle.begin();

    try {
      const response = await fetch('/admin/telegram/api/qr/start', {
        method: 'POST',
        credentials: 'include',
        signal: requestToken.signal,
        headers: { accept: 'application/json', 'content-type': 'application/json' },
        body: JSON.stringify({ session_id: nextSessionId })
      });
      const payload = await readJson(response);
      if (!response.ok) throw new Error(detailMessage(payload, 'Could not start QR sign-in.'));
      const result = payload as { attempt_id?: unknown; qr_url?: unknown; expires_at?: unknown };
      if (typeof result.attempt_id !== 'string' || typeof result.qr_url !== 'string') {
        throw new Error('Telegram QR sign-in returned an invalid response.');
      }
      if (!requestLifecycle.isCurrent(requestToken) || !modalOpen) return;
      openModal({
        kind: 'qr',
        sessionId: nextSessionId,
        attemptId: result.attempt_id,
        qrUrl: result.qr_url,
        expiresAt: typeof result.expires_at === 'string' ? result.expires_at : undefined,
        message: 'Waiting for scan…'
      }, requestToken);
    } catch (caught) {
      if (requestLifecycle.isCurrent(requestToken) && modalOpen && !(caught instanceof Error && caught.name === 'AbortError')) {
        error = caught instanceof Error ? safeOperatorMessage(caught.message) : 'Could not start QR sign-in.';
      }
    } finally {
      if (requestLifecycle.isCurrent(requestToken)) refreshing = false;
    }
  }

  function openModal(step: TelegramQrLoginStep, requestToken?: QrRequestToken): void {
    if (requestToken && (!requestLifecycle.isCurrent(requestToken) || !modalOpen)) return;
    if (!requestToken) requestLifecycle.cancel();
    stopPolling();
    clearRefreshTimer();
    sessionId = step.sessionId;
    attemptId = step.attemptId;
    qrUrl = step.qrUrl;
    expiresAt = step.expiresAt ?? null;
    message = 'Waiting for scan…';
    refreshing = Boolean(step.refreshing);
    error = null;
    modalOpen = true;
    if (browser) {
      scheduleRefresh();
      void pollStatus();
    }
  }

  function clearRefreshTimer(): void {
    if (refreshTimer) clearTimeout(refreshTimer);
    refreshTimer = null;
  }

  function stopPolling(): void {
    pollAbort?.abort();
    pollAbort = null;
    polling = false;
  }

  function scheduleRefresh(): void {
    if (!browser || !expiresAt || !sessionId) return;
    clearRefreshTimer();
    const expiry = new Date(expiresAt).getTime();
    if (!Number.isFinite(expiry)) return;
    refreshTimer = setTimeout(() => void refreshQr(), Math.max(5_000, expiry - Date.now() - 15_000));
  }

  async function refreshQr(): Promise<void> {
    if (!sessionId || refreshing) return;
    const currentSessionId = sessionId;
    refreshing = true;
    error = null;
    stopPolling();
    const requestToken = requestLifecycle.begin();
    try {
      const response = await fetch('/admin/telegram/api/qr/start', {
        method: 'POST',
        credentials: 'include',
        signal: requestToken.signal,
        headers: { accept: 'application/json', 'content-type': 'application/json' },
        body: JSON.stringify({ session_id: currentSessionId })
      });
      const payload = await readJson(response);
      if (!response.ok) throw new Error(detailMessage(payload, 'Could not refresh the QR code.'));
      const result = payload as { attempt_id?: unknown; qr_url?: unknown; expires_at?: unknown };
      if (typeof result.attempt_id !== 'string' || typeof result.qr_url !== 'string') {
        throw new Error('Telegram QR refresh returned an invalid response.');
      }
      if (!requestLifecycle.isCurrent(requestToken) || !modalOpen || sessionId !== currentSessionId) return;
      attemptId = result.attempt_id;
      qrUrl = result.qr_url;
      expiresAt = typeof result.expires_at === 'string' ? result.expires_at : null;
      message = 'Waiting for scan…';
      scheduleRefresh();
      void pollStatus();
    } catch (caught) {
      if (requestLifecycle.isCurrent(requestToken) && modalOpen && !(caught instanceof Error && caught.name === 'AbortError')) {
        error = caught instanceof Error ? safeOperatorMessage(caught.message) : 'Could not refresh the QR code.';
      }
    } finally {
      if (requestLifecycle.isCurrent(requestToken)) refreshing = false;
    }
  }

  async function pollStatus(): Promise<void> {
    if (!browser || !sessionId || !attemptId || polling) return;
    const controller = new AbortController();
    pollAbort = controller;
    polling = true;
    try {
      while (modalOpen && sessionId && attemptId && !controller.signal.aborted) {
        const currentSessionId = sessionId;
        const currentAttemptId = attemptId;
        const response = await fetch('/admin/telegram/api/qr/complete', {
          method: 'POST',
          credentials: 'include',
          signal: controller.signal,
          headers: { accept: 'application/json', 'content-type': 'application/json' },
          body: JSON.stringify({ session_id: currentSessionId, attempt_id: currentAttemptId })
        });
        const payload = await readJson(response);
        if (!response.ok) throw new Error(detailMessage(payload, 'Could not check QR sign-in status.'));
        const result = payload as AdminTelegramLoginQrStatusRead;
        if (result.status === 'pending') {
          message = 'Waiting for scan…';
          continue;
        }
        clearRefreshTimer();
        if (result.status === 'password_required') {
          onPasswordRequired({
            kind: 'password',
            method: 'qr',
            sessionId: currentSessionId,
            attemptId: currentAttemptId,
            message: safeOperatorMessage(result.message)
          });
          modalOpen = false;
          break;
        }
        if (result.status === 'completed') {
          message = safeOperatorMessage(result.message);
          modalOpen = false;
          await invalidateAll();
          break;
        }
        throw new Error('Telegram QR sign-in returned an unknown status.');
      }
    } catch (caught) {
      if (caught instanceof Error && caught.name === 'AbortError') return;
      error = caught instanceof Error ? safeOperatorMessage(caught.message) : 'Could not check QR sign-in status.';
    } finally {
      if (pollAbort === controller) {
        pollAbort = null;
        polling = false;
      }
    }
  }

  async function readJson(response: Response): Promise<unknown> {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }

  function detailMessage(payload: unknown, fallback: string): string {
    if (payload && typeof payload === 'object' && 'detail' in payload && typeof payload.detail === 'string') return safeOperatorMessage(payload.detail);
    return fallback;
  }

  function qrImageSrc(value: string): string | null {
    try {
      return qrSvgDataUri(value);
    } catch {
      return null;
    }
  }

  function closeDialog(): void {
    requestLifecycle.cancel();
    stopPolling();
    clearRefreshTimer();
    modalOpen = false;
  }
</script>

<Dialog.Root bind:open={modalOpen}>
  <Dialog.Content class="w-[min(92vw,34rem)]" aria-labelledby="telegram-qr-login-title" aria-describedby="telegram-qr-login-description">
    <Dialog.Title id="telegram-qr-login-title" class="m-0 text-3xl font-black tracking-[-0.05em]">Connect with Telegram QR</Dialog.Title>
    <Dialog.Description id="telegram-qr-login-description" class="m-0 text-sm text-muted">
      Open Telegram → Settings → Devices → Link Desktop Device, then scan this code. MemeExpert continues automatically.
    </Dialog.Description>

    <section class="grid gap-4 rounded-3xl border border-line bg-soft/40 p-4" aria-label="Telegram QR sign-in waiting state">
      {#if refreshing}
        <LoadingState label="Loading a new QR code" />
      {/if}

      {#if qrUrl && !refreshing}
        {@const image = qrImageSrc(qrUrl)}
        <div class="rounded-2xl border border-line bg-paper p-3">
          {#if image}
            <img src={image} alt="Telegram sign-in QR code" class="mx-auto aspect-square w-full max-w-[260px]" />
          {:else}
            <p class="m-0 text-sm text-muted">The QR image could not be rendered. Refresh to request another code.</p>
          {/if}
        </div>
        <div class="grid gap-2 text-sm text-muted">
          <p class="m-0 font-extrabold text-ink">{message}</p>
          <p class="m-0">The code refreshes automatically before it expires.</p>
          <a class="font-extrabold text-ink underline" href={qrUrl}>Open Telegram sign-in link</a>
        </div>
      {:else if !refreshing}
        <LoadingState label="Loading a new QR code" />
      {/if}

      {#if polling && !refreshing}
        <LoadingState label="Waiting for scan…" />
      {/if}

      {#if error}
        <Notice tone="danger" role="alert">{error}</Notice>
      {/if}

      <div class="flex flex-wrap gap-2">
        <Button type="button" variant="secondary" onclick={refreshQr} disabled={refreshing}>Refresh QR</Button>
        <Button type="button" variant="ghost" onclick={closeDialog}>Close</Button>
      </div>
    </section>
  </Dialog.Content>
</Dialog.Root>
