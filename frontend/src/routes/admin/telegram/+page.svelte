<script lang="ts">
  import { browser } from '$app/environment';
  import { invalidateAll } from '$app/navigation';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { qrSvgDataUri } from '$lib/qr/svg';
  import { ActionLink, Badge, Button, EmptyState, FormRow, Input, LoadingState, Notice, Select, Textarea } from '$lib/ui';
  import * as Dialog from '$lib/ui/dialog';
  import type { AdminTelegramLoginQrStatusRead, AdminTelegramSessionRead, TelegramSessionStatus } from '$lib/api/types';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

  const sessionStatuses: TelegramSessionStatus[] = ['active', 'auth_required', 'flood_wait', 'quarantined', 'stopped'];

  type LoginStepAction =
    | { kind: 'qr'; sessionId: string; attemptId: string; qrUrl: string; expiresAt?: string; message: string; error?: boolean; refreshing?: boolean }
    | { kind: 'phone_code'; sessionId: string; attemptId: string; phoneHint?: string | null; expiresAt?: string; message: string; error?: boolean }
    | { kind: 'password'; method: 'phone' | 'qr'; sessionId: string; attemptId: string; message: string; error?: boolean };

  type QrLoginStep = Extract<LoginStepAction, { kind: 'qr' }>;
  type PasswordLoginStep = Extract<LoginStepAction, { kind: 'password' }>;

  let qrPasswordStep = $state<PasswordLoginStep | null>(null);
  let qrModalOpen = $state(false);
  let qrSessionId = $state<string | null>(null);
  let qrAttemptId = $state<string | null>(null);
  let qrUrl = $state<string | null>(null);
  let qrExpiresAt = $state<string | null>(null);
  let qrMessage = $state('Waiting for scan…');
  let qrRefreshing = $state(false);
  let qrPolling = $state(false);
  let qrError = $state<string | null>(null);
  let qrOpenedAttemptFromForm = $state<string | null>(null);
  let qrPollAbort: AbortController | null = null;
  let qrRefreshTimer: ReturnType<typeof setTimeout> | null = null;

  $effect(() => {
    const qrStep = isQrLoginStep(form) ? form : null;
    if (!qrStep || qrOpenedAttemptFromForm === qrStep.attemptId) return;
    qrOpenedAttemptFromForm = qrStep.attemptId;
    openQrModal(qrStep);
  });

  $effect(() => {
    if (qrModalOpen) return;
    stopQrPolling();
    clearQrRefreshTimer();
  });

  function sessionLabel(session: AdminTelegramSessionRead): string {
    return session.display_name === session.name ? session.name : `${session.display_name} (${session.name})`;
  }

  function formatTimestamp(value: string | null): string {
    if (!value) return 'none';
    return value.replace('T', ' ').replace(/\.\d+(Z|[+-]\d\d:\d\d)?$/, '$1');
  }

  function accountCopy(session: AdminTelegramSessionRead): string {
    const parts = [
      session.account_username ? `@${session.account_username}` : null,
      session.account_user_id === null ? null : `user ${session.account_user_id}`,
      session.account_phone_hint ? `phone ${session.account_phone_hint}` : null
    ].filter(Boolean);
    return parts.length > 0 ? parts.join(' · ') : 'no account metadata';
  }

  function loginStepFor(session: AdminTelegramSessionRead): LoginStepAction | null {
    if (qrPasswordStep?.sessionId === session.id) {
      return qrPasswordStep;
    }
    const candidate = form as LoginStepAction | null | undefined;
    if (!candidate || candidate.sessionId !== session.id) {
      return null;
    }
    return candidate.kind === 'phone_code' || candidate.kind === 'password' ? candidate : null;
  }

  function isQrLoginStep(value: ActionData | LoginStepAction | null | undefined): value is QrLoginStep {
    const candidate = value as Partial<QrLoginStep> | null | undefined;
    return candidate?.kind === 'qr' && typeof candidate.sessionId === 'string' && typeof candidate.attemptId === 'string' && typeof candidate.qrUrl === 'string';
  }

  function openQrModal(step: QrLoginStep): void {
    stopQrPolling();
    qrPasswordStep = null;
    qrSessionId = step.sessionId;
    qrAttemptId = step.attemptId;
    qrUrl = step.qrUrl;
    qrExpiresAt = step.expiresAt ?? null;
    qrMessage = 'Waiting for scan…';
    qrRefreshing = Boolean(step.refreshing);
    qrError = null;
    qrModalOpen = true;
    if (browser) {
      scheduleQrRefresh();
      void pollQrStatus();
    }
  }

  function clearQrRefreshTimer(): void {
    if (qrRefreshTimer) clearTimeout(qrRefreshTimer);
    qrRefreshTimer = null;
  }

  function stopQrPolling(): void {
    qrPollAbort?.abort();
    qrPollAbort = null;
    qrPolling = false;
  }

  function scheduleQrRefresh(): void {
    if (!browser || !qrExpiresAt || !qrSessionId) return;
    clearQrRefreshTimer();
    const expiresMs = new Date(qrExpiresAt).getTime();
    if (!Number.isFinite(expiresMs)) return;
    const refreshInMs = Math.max(5_000, expiresMs - Date.now() - 15_000);
    qrRefreshTimer = setTimeout(() => void refreshQr(), refreshInMs);
  }

  async function refreshQr(): Promise<void> {
    if (!qrSessionId || qrRefreshing) return;
    qrRefreshing = true;
    qrError = null;
    stopQrPolling();
    try {
      const response = await fetch('/admin/telegram/api/qr/start', {
        method: 'POST',
        credentials: 'include',
        headers: { accept: 'application/json', 'content-type': 'application/json' },
        body: JSON.stringify({ session_id: qrSessionId })
      });
      const payload = await readJson(response);
      if (!response.ok) throw new Error(detailMessage(payload, 'Could not refresh QR code.'));
      const result = payload as { attempt_id?: unknown; qr_url?: unknown; expires_at?: unknown };
      if (typeof result.attempt_id !== 'string' || typeof result.qr_url !== 'string') {
        throw new Error('Telegram QR refresh returned an invalid response.');
      }
      qrAttemptId = result.attempt_id;
      qrUrl = result.qr_url;
      qrExpiresAt = typeof result.expires_at === 'string' ? result.expires_at : null;
      qrMessage = 'Waiting for scan…';
      scheduleQrRefresh();
      void pollQrStatus();
    } catch (error) {
      qrError = error instanceof Error ? error.message : 'Could not refresh QR code.';
    } finally {
      qrRefreshing = false;
    }
  }

  async function startExistingQrLogin(event: SubmitEvent): Promise<void> {
    const formElement = event.currentTarget instanceof HTMLFormElement ? event.currentTarget : null;
    if (!formElement) return;
    const data = new FormData(formElement);
    const sessionId = String(data.get('session_id') ?? '').trim();
    if (!sessionId) return;

    event.preventDefault();
    stopQrPolling();
    clearQrRefreshTimer();
    qrPasswordStep = null;
    qrSessionId = sessionId;
    qrAttemptId = null;
    qrUrl = null;
    qrExpiresAt = null;
    qrMessage = 'Loading new QR code…';
    qrError = null;
    qrRefreshing = true;
    qrModalOpen = true;

    try {
      const response = await fetch('/admin/telegram/api/qr/start', {
        method: 'POST',
        credentials: 'include',
        headers: { accept: 'application/json', 'content-type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId })
      });
      const payload = await readJson(response);
      if (!response.ok) throw new Error(detailMessage(payload, 'Could not start QR login.'));
      const result = payload as { attempt_id?: unknown; qr_url?: unknown; expires_at?: unknown };
      if (typeof result.attempt_id !== 'string' || typeof result.qr_url !== 'string') {
        throw new Error('Telegram QR login returned an invalid response.');
      }
      openQrModal({
        kind: 'qr',
        sessionId,
        attemptId: result.attempt_id,
        qrUrl: result.qr_url,
        expiresAt: typeof result.expires_at === 'string' ? result.expires_at : undefined,
        message: 'Waiting for scan…'
      });
    } catch (error) {
      qrError = error instanceof Error ? error.message : 'Could not start QR login.';
    } finally {
      qrRefreshing = false;
    }
  }

  async function pollQrStatus(): Promise<void> {
    if (!browser || !qrSessionId || !qrAttemptId || qrPolling) return;
    const controller = new AbortController();
    qrPollAbort = controller;
    qrPolling = true;
    try {
      while (qrModalOpen && qrSessionId && qrAttemptId && !controller.signal.aborted) {
        const sessionId = qrSessionId;
        const attemptId = qrAttemptId;
        const response = await fetch('/admin/telegram/api/qr/complete', {
          method: 'POST',
          credentials: 'include',
          signal: controller.signal,
          headers: { accept: 'application/json', 'content-type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId, attempt_id: attemptId })
        });
        const payload = await readJson(response);
        if (!response.ok) throw new Error(detailMessage(payload, 'Could not check QR login status.'));
        const result = payload as AdminTelegramLoginQrStatusRead;
        if (result.status === 'pending') {
          qrMessage = 'Waiting for scan…';
          continue;
        }
        clearQrRefreshTimer();
        if (result.status === 'password_required') {
          qrPasswordStep = {
            kind: 'password',
            method: 'qr',
            sessionId,
            attemptId,
            message: result.message
          };
          qrMessage = result.message;
          qrModalOpen = false;
          break;
        }
        if (result.status === 'completed') {
          qrMessage = result.message;
          qrModalOpen = false;
          await invalidateAll();
          break;
        }
        throw new Error('Telegram QR login returned an unknown status.');
      }
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') return;
      qrError = error instanceof Error ? error.message : 'Could not check QR login status.';
    } finally {
      if (qrPollAbort === controller) {
        qrPollAbort = null;
        qrPolling = false;
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
    if (payload && typeof payload === 'object' && 'detail' in payload && typeof payload.detail === 'string') {
      return payload.detail;
    }
    return fallback;
  }

  function qrImageSrc(qrUrl: string): string | null {
    try {
      return qrSvgDataUri(qrUrl);
    } catch {
      return null;
    }
  }

  function expiresCopy(value: string | undefined): string {
    return value ? formatTimestamp(value) : 'soon';
  }
</script>

<section>
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div>
      <Badge>Signed in as {data.adminUser.email || data.adminUser.id}</Badge>
      <h1 class="my-3 text-[clamp(2.4rem,8vw,5rem)] font-black leading-[0.9] tracking-[-0.075em]">Telegram admin</h1>
      <p class="m-0 max-w-3xl text-muted">
        Manage DB-backed Telegram sessions and source-channel assignment without exposing Telegram login secrets.
      </p>
    </div>
    <div class="flex flex-wrap gap-2">
      <ActionLink href="/admin/sources" variant="secondary">Manage {data.telegramAdmin.sourceCount} {data.telegramAdmin.sourceCount === 1 ? 'source' : 'sources'}</ActionLink>
      <ActionLink href="/admin" variant="secondary">Back to admin tools</ActionLink>
    </div>
  </div>
</section>

{#if form?.message}
  <Notice tone={form.error ? 'danger' : 'success'} role={form.error ? 'alert' : 'status'}>{form.message}</Notice>
{/if}

{#if data.loadError}
  <Notice role="alert" tone="danger">{data.loadError}</Notice>
{/if}

<Dialog.Root bind:open={qrModalOpen}>
  <Dialog.Content class="w-[min(92vw,34rem)]" aria-labelledby="telegram-qr-login-title" aria-describedby="telegram-qr-login-description">
    <Dialog.Title id="telegram-qr-login-title" class="m-0 text-3xl font-black tracking-[-0.05em]">Log in with Telegram QR</Dialog.Title>
    <Dialog.Description id="telegram-qr-login-description" class="m-0 text-sm text-muted">
      Open Telegram → Settings → Devices → Link Desktop Device. Scan this code; MemeExpert continues automatically.
    </Dialog.Description>

    <section class="grid gap-4 rounded-3xl border border-line bg-soft/40 p-4" aria-label="Telegram QR login waiting state">
      {#if qrRefreshing}
        <LoadingState label="Loading new QR code" />
      {/if}

      {#if qrUrl && !qrRefreshing}
        {@const modalQrImage = qrImageSrc(qrUrl)}
        <div class="rounded-2xl border border-line bg-paper p-3">
          {#if modalQrImage}
            <img src={modalQrImage} alt="Telegram login QR code" class="mx-auto aspect-square w-full max-w-[260px]" />
          {:else}
            <p class="m-0 text-sm text-muted">QR image could not be rendered. Use the fallback link.</p>
          {/if}
        </div>
        <div class="grid gap-2 text-sm text-muted">
          <p class="m-0 font-extrabold text-ink">{qrMessage || 'Waiting for scan…'}</p>
          <p class="m-0">QR refreshes automatically before it expires{qrExpiresAt ? ` · expires ${expiresCopy(qrExpiresAt)}` : ''}.</p>
          <a class="font-extrabold text-ink underline" href={qrUrl}>Open Telegram login link</a>
        </div>
      {:else if !qrRefreshing}
        <LoadingState label="Loading new QR code" />
      {/if}

      {#if qrPolling && !qrRefreshing}
        <LoadingState label="Waiting for scan…" />
      {/if}

      {#if qrError}
        <Notice tone="danger" role="alert">{qrError}</Notice>
      {/if}

      <div class="flex flex-wrap gap-2">
        <Button type="button" variant="secondary" onclick={refreshQr} disabled={qrRefreshing}>Refresh QR now</Button>
      </div>
    </section>
  </Dialog.Content>
</Dialog.Root>

<div class="my-5">
  <AdminPanel title="Start New Login">
    <p class="m-0 text-sm text-muted">Authenticate with Telegram only. Pick one method; MemeExpert creates the session row and continues the next step on that new session card.</p>
    <div class="grid gap-3 md:grid-cols-2">
      <form method="POST" action="?/startQrLogin" class="grid content-start gap-3 rounded-2xl border border-line bg-soft/40 p-3">
        <strong>Log in with QR</strong>
        <p class="m-0 text-sm text-muted">Show a QR code you can scan from Telegram → Devices.</p>
        <Button type="submit" variant="secondary">Show QR code</Button>
      </form>
      <form method="POST" action="?/startPhoneLogin" class="grid content-start gap-3 rounded-2xl border border-line bg-soft/40 p-3">
        <strong>Log in with phone</strong>
        <p class="m-0 text-sm text-muted">Enter the phone number once, then continue with the Telegram code.</p>
        <FormRow label="Phone number"><Input name="phone_number" autocomplete="tel" placeholder="+15551234567" required /></FormRow>
        <Button type="submit" variant="secondary">Continue</Button>
      </form>
    </div>
  </AdminPanel>
</div>

<AdminPanel title="Sessions">
  {#if data.telegramAdmin.sessions.length === 0}
    <EmptyState title="No Telegram sessions" message="Start QR or phone login; the session name is derived automatically after Telegram login succeeds." />
  {:else}
    <div class="grid gap-4">
      {#each data.telegramAdmin.sessions as session (session.id)}
        {@const loginStep = loginStepFor(session)}
        <article class="grid gap-4 rounded-3xl border border-line bg-paper p-4">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 class="m-0 text-xl font-black tracking-[-0.03em]">{session.display_name}</h3>
              <p class="m-0 text-sm text-muted">{session.name} · ID {session.id}</p>
            </div>
            <div class="flex flex-wrap gap-2">
              <Badge tone={session.status === 'active' ? 'success' : 'neutral'}>{session.status}</Badge>
              <Badge class={session.enabled ? '' : 'border-danger-line bg-danger-surface text-danger'}>{session.enabled ? 'enabled' : 'disabled'}</Badge>
              <Badge>{session.owned_channel_count} channels</Badge>
              <Badge>{session.has_string_session ? 'session key stored' : 'login required'}</Badge>
            </div>
          </div>

          <div class="grid gap-2 text-sm md:grid-cols-2 xl:grid-cols-4">
            <p class="m-0"><strong>Policy:</strong> catch-up {session.catchup_enabled ? 'on' : 'off'} · live {session.live_enabled ? 'on' : 'off'} · engagement {session.engagement_enabled ? 'on' : 'off'} · {session.max_requests_per_second}/sec</p>
            <p class="m-0"><strong>Heartbeat:</strong> {formatTimestamp(session.last_heartbeat_at)} · listener {formatTimestamp(session.live_listener_started_at)}</p>
            <p class="m-0"><strong>Health:</strong> flood wait {formatTimestamp(session.flood_wait_until)} · quarantined {formatTimestamp(session.quarantined_at)}</p>
            <p class="m-0"><strong>Account:</strong> {accountCopy(session)}</p>
          </div>

          {#if session.last_error_class || session.last_error_text}
            <div class="rounded-2xl border border-danger-line bg-danger-surface p-3 text-sm text-danger">
              <strong>{session.last_error_class ?? 'Last error'}</strong>
              {#if session.last_error_text}<p class="m-0 break-words">{session.last_error_text}</p>{/if}
            </div>
          {/if}

          <div class="grid gap-3 rounded-2xl border border-line bg-soft/40 p-4">
            <div>
              <strong>Telegram login</strong>
              <p class="m-0 text-sm text-muted">Use the same simple flow as Telegram: phone → code → password if needed, or QR → password if needed.</p>
            </div>

            {#if loginStep?.kind === 'phone_code'}
              <form method="POST" action="?/completePhoneCodeLogin" class="grid gap-3 md:max-w-xl">
                <input type="hidden" name="session_id" value={session.id} />
                <input type="hidden" name="attempt_id" value={loginStep.attemptId} />
                <div>
                  <strong>Step 2 of 3 · Enter code</strong>
                  <p class="m-0 text-sm text-muted">Telegram sent a code to {loginStep.phoneHint ? `phone ${loginStep.phoneHint}` : 'the account phone'}. Expires {expiresCopy(loginStep.expiresAt)}.</p>
                </div>
                <FormRow label="Telegram code"><Input name="code" autocomplete="one-time-code" inputmode="numeric" required autofocus /></FormRow>
                <Button type="submit">Continue</Button>
              </form>
            {:else if loginStep?.kind === 'password'}
              <form method="POST" action="?/completePhonePasswordLogin" class="grid gap-3 md:max-w-xl">
                <input type="hidden" name="session_id" value={session.id} />
                <input type="hidden" name="attempt_id" value={loginStep.attemptId} />
                <input type="hidden" name="method" value={loginStep.method} />
                <div>
                  <strong>{loginStep.method === 'qr' ? 'Step 2 of 2' : 'Step 3 of 3'} · Enter password</strong>
                  <p class="m-0 text-sm text-muted">Telegram requires the account password to finish this login.</p>
                </div>
                <FormRow label="Telegram password"><Input name="password" type="password" autocomplete="current-password" required autofocus /></FormRow>
                <Button type="submit">Finish login</Button>
              </form>
            {:else}
              <div class="grid gap-3 md:grid-cols-2">
                <form method="POST" action="?/startPhoneLogin" class="grid content-start gap-3 rounded-2xl border border-line bg-paper p-3">
                  <input type="hidden" name="session_id" value={session.id} />
                  <strong>Log in with phone</strong>
                  <p class="m-0 text-sm text-muted">Enter the phone number, then the Telegram code.</p>
                  <FormRow label="Phone number"><Input name="phone_number" autocomplete="tel" placeholder="+15551234567" required /></FormRow>
                  <Button type="submit" variant="secondary">Continue</Button>
                </form>
                <form method="POST" action="?/startQrLogin" onsubmit={startExistingQrLogin} class="grid content-start gap-3 rounded-2xl border border-line bg-paper p-3">
                  <input type="hidden" name="session_id" value={session.id} />
                  <strong>Log in with QR</strong>
                  <p class="m-0 text-sm text-muted">Show a QR code and scan it with Telegram.</p>
                  <Button type="submit" variant="secondary">Show QR code</Button>
                </form>
              </div>
            {/if}
          </div>

          <div class="grid gap-3 xl:grid-cols-[minmax(0,1.4fr)_minmax(260px,0.7fr)_minmax(260px,0.7fr)]">
            <form method="POST" action="?/updateSession" class="grid gap-3 rounded-2xl border border-line bg-soft/40 p-3">
              <input type="hidden" name="session_id" value={session.id} />
              <strong>Patch policy/status</strong>
              <div class="grid gap-2 md:grid-cols-3">
                <FormRow label="Display name"><Input name="display_name" value={session.display_name} required /></FormRow>
                <FormRow label="Status">
                  <Select name="status">
                    {#each sessionStatuses as status}
                      <option value={status} selected={status === session.status}>{status}</option>
                    {/each}
                  </Select>
                </FormRow>
                <FormRow label="Max requests/sec"><Input name="max_requests_per_second" type="number" min="0.1" step="0.1" value={session.max_requests_per_second} required /></FormRow>
              </div>
              <div class="grid gap-2 md:grid-cols-3">
                <FormRow label="Flood wait until"><Input name="flood_wait_until" value={session.flood_wait_until ?? ''} placeholder="ISO datetime or blank" /></FormRow>
                <FormRow label="Last error class"><Input name="last_error_class" value={session.last_error_class ?? ''} /></FormRow>
                <FormRow label="Audit note"><Input name="note" placeholder="optional" /></FormRow>
              </div>
              <FormRow label="Last error text"><Textarea name="last_error_text" rows={2} value={session.last_error_text ?? ''} /></FormRow>
              <div class="grid gap-2 md:grid-cols-3">
                <label class="inline-flex items-center gap-2 text-chiptext"><input name="enabled" type="checkbox" checked={session.enabled} /> Enabled</label>
                <label class="inline-flex items-center gap-2 text-chiptext"><input name="catchup_enabled" type="checkbox" checked={session.catchup_enabled} /> Catch-up</label>
                <label class="inline-flex items-center gap-2 text-chiptext"><input name="live_enabled" type="checkbox" checked={session.live_enabled} /> Live</label>
                <label class="inline-flex items-center gap-2 text-chiptext"><input name="engagement_enabled" type="checkbox" checked={session.engagement_enabled} /> Engagement</label>
                <label class="inline-flex items-center gap-2 text-chiptext"><input name="clear_error" type="checkbox" /> Clear error</label>
              </div>
              <Button type="submit">Save session</Button>
            </form>

            <form method="POST" action="?/validateSession" class="grid content-start gap-3 rounded-2xl border border-line bg-soft/40 p-3">
              <input type="hidden" name="session_id" value={session.id} />
              <strong>Validate access</strong>
              <p class="m-0 text-sm text-muted">Check this Telegram account. Manage and validate source assignment from Sources.</p>
              <Input name="note" placeholder="validation note" />
              <Button type="submit" variant="secondary">Validate</Button>
            </form>

            <form method="POST" action="?/deleteSession" class="grid content-start gap-3 rounded-2xl border border-danger-line bg-danger-surface p-3">
              <input type="hidden" name="session_id" value={session.id} />
              <strong>Delete session</strong>
              <p class="m-0 text-sm text-danger">Assigned channels become orphaned and non-indexable. Paste the session id to confirm.</p>
              <Input name="confirmation" placeholder={session.id} autocomplete="off" required />
              <Textarea name="note" rows={2} placeholder="required operator context is recommended" />
              <Button type="submit" variant="danger">Delete and orphan channels</Button>
            </form>
          </div>
        </article>
      {/each}
    </div>
  {/if}
</AdminPanel>
