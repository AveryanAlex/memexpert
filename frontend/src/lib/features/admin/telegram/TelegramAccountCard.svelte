<script lang="ts">
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import type { AdminTelegramSessionRead, TelegramSessionStatus } from '$lib/api/types';
  import { Badge, Button, FormRow, Input, Notice, Select, Textarea } from '$lib/ui';
  import { safePhoneHint, toTelegramAccountViewModel, type TelegramLoginState } from './view-model';

  const sessionStatuses: TelegramSessionStatus[] = ['active', 'auth_required', 'flood_wait', 'quarantined', 'stopped'];

  let {
    account,
    loadedAt,
    loginState = null,
    onStartQrLogin
  }: {
    account: AdminTelegramSessionRead;
    loadedAt: string;
    loginState?: TelegramLoginState | null;
    onStartQrLogin?: (event: SubmitEvent) => void;
  } = $props();

  const model = $derived(toTelegramAccountViewModel(account, new Date(loadedAt)));
  const phoneHint = $derived(loginState?.kind === 'phone_code' ? safePhoneHint(loginState.phoneHint) : null);
  const passwordPhoneHint = $derived(loginState?.kind === 'password' ? safePhoneHint(loginState.phoneHint) : null);
  const loginFailed = $derived(loginState?.kind === 'login_error');
  const sourceImpactCopy = $derived(
    `Permanently deletes this database account record. ${account.owned_channel_count} ${account.owned_channel_count === 1 ? 'assigned source becomes' : 'assigned sources become'} unassigned and ingestion is disabled.`
  );
</script>

<article class="grid gap-4 rounded-3xl border border-line bg-paper p-4" aria-labelledby={`telegram-account-${account.id}`}>
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div class="min-w-0">
      <h3 id={`telegram-account-${account.id}`} class="m-0 text-xl font-black tracking-[-0.03em]">{model.displayName}</h3>
      <p class="m-0 text-sm text-muted">{model.identity}</p>
    </div>
    <span role="status" aria-label={`Account status: ${model.status}`}>
      <Badge tone={model.status === 'Ready' ? 'success' : 'neutral'}>{model.status}</Badge>
    </span>
  </div>

  <dl class="grid gap-2 text-sm sm:grid-cols-2">
    <div><dt class="font-extrabold">Sources</dt><dd class="m-0 text-muted">{model.sourceCountLabel}</dd></div>
    <div><dt class="font-extrabold">Last heartbeat</dt><dd class="m-0 text-muted">{model.heartbeatLabel}</dd></div>
  </dl>
  <p class="m-0 text-sm text-muted">{model.statusDetail}</p>

  {#if model.errorSummary}
    <Notice tone="danger" role="alert"><strong>Needs attention:</strong> {model.errorSummary}</Notice>
  {/if}

  {#if loginState?.kind === 'phone_code'}
    <form method="POST" action="?/completePhoneCodeLogin" class="grid gap-3 rounded-2xl border border-line bg-soft/40 p-4 md:max-w-xl">
      <input type="hidden" name="session_id" value={account.id} />
      <input type="hidden" name="attempt_id" value={loginState.attemptId} />
      <input type="hidden" name="phone_hint" value={phoneHint ?? ''} />
      <div>
        <strong>Enter the Telegram code</strong>
        <p class="m-0 text-sm text-muted">Telegram sent a code to {phoneHint ?? 'the account phone'}.</p>
      </div>
      {#if loginState.error}
        <Notice tone="danger" role="alert">{loginState.message}</Notice>
      {/if}
      <FormRow label="Telegram code">
        <Input name="code" autocomplete="one-time-code" inputmode="numeric" required autofocus />
      </FormRow>
      <Button type="submit">Continue</Button>
    </form>
  {:else if loginState?.kind === 'password'}
    <form method="POST" action="?/completePhonePasswordLogin" class="grid gap-3 rounded-2xl border border-line bg-soft/40 p-4 md:max-w-xl">
      <input type="hidden" name="session_id" value={account.id} />
      <input type="hidden" name="attempt_id" value={loginState.attemptId} />
      <input type="hidden" name="method" value={loginState.method} />
      <input type="hidden" name="phone_hint" value={passwordPhoneHint ?? ''} />
      <div>
        <strong>Enter the Telegram password</strong>
        <p class="m-0 text-sm text-muted">Telegram requires the account password to finish connecting.</p>
      </div>
      {#if loginState.error}
        <Notice tone="danger" role="alert">{loginState.message}</Notice>
      {/if}
      <FormRow label="Telegram password">
        <Input name="password" type="password" autocomplete="current-password" required autofocus />
      </FormRow>
      <Button type="submit">Finish connecting</Button>
    </form>
  {:else if loginFailed || model.primaryAction === 'Connect'}
    {#if loginFailed}
      <Notice tone="danger" role="alert"><strong>Sign-in did not finish.</strong> {loginState?.message ?? 'Telegram sign-in could not continue.'} Restart the connection below.</Notice>
    {/if}
    <div class="grid gap-3 rounded-2xl border border-line bg-soft/40 p-4">
      <form method="POST" action="?/startQrLogin" onsubmit={onStartQrLogin} class="flex flex-wrap gap-2">
        <input type="hidden" name="session_id" value={account.id} />
        <Button type="submit" variant="secondary">{loginFailed ? 'Restart with QR' : 'Connect with QR'}</Button>
      </form>
      <AdvancedSection title="Use phone instead" description="Use this account's phone number only if QR sign-in is not available.">
        <form method="POST" action="?/startPhoneLogin" class="grid max-w-xl gap-3">
          <input type="hidden" name="session_id" value={account.id} />
          <FormRow label="Phone number">
            <Input name="phone_number" autocomplete="tel" inputmode="tel" placeholder="Enter full phone number" required />
          </FormRow>
          <Button type="submit" variant="secondary">Continue with phone</Button>
        </form>
      </AdvancedSection>
    </div>
  {:else if model.primaryAction === 'Validate'}
    <form method="POST" action="?/validateSession" class="flex flex-wrap gap-2">
      <input type="hidden" name="session_id" value={account.id} />
      <Button type="submit" variant="secondary">Validate account</Button>
    </form>
    {#if model.canReconnect}
      <AdvancedSection title="Reconnect account" description="Use a new QR or phone sign-in only when validation cannot repair this account.">
        <div class="grid gap-3">
          <form method="POST" action="?/startQrLogin" onsubmit={onStartQrLogin} class="flex flex-wrap gap-2">
            <input type="hidden" name="session_id" value={account.id} />
            <Button type="submit" variant="secondary">Reconnect with QR</Button>
          </form>
          <form method="POST" action="?/startPhoneLogin" class="grid max-w-xl gap-3">
            <input type="hidden" name="session_id" value={account.id} />
            <FormRow label="Phone number">
              <Input name="phone_number" autocomplete="tel" inputmode="tel" placeholder="Enter full phone number" required />
            </FormRow>
            <Button type="submit" variant="secondary">Continue with phone</Button>
          </form>
        </div>
      </AdvancedSection>
    {/if}
  {:else if model.primaryAction === 'Enable' || model.primaryAction === 'Resume'}
    <form method="POST" action="?/repairSession" class="flex flex-wrap gap-2">
      <input type="hidden" name="session_id" value={account.id} />
      <input type="hidden" name="repair" value={model.primaryAction === 'Enable' ? 'enable' : 'resume'} />
      <Button type="submit" variant="secondary">{model.primaryAction === 'Enable' ? 'Enable account' : 'Resume account'}</Button>
    </form>
  {:else}
    <Notice role="status">No Telegram action is available while this account is rate-limited. Check the deadline in Diagnostics.</Notice>
  {/if}

  <div class="grid gap-3">
    <AdvancedSection title="Diagnostics" description="Technical account details and crawler checkpoints for troubleshooting.">
      <dl class="m-0 grid gap-2 text-sm md:grid-cols-2">
        <div><dt class="font-extrabold">Account ID</dt><dd class="m-0 break-all text-muted">{model.id}</dd></div>
        <div><dt class="font-extrabold">Technical account name</dt><dd class="m-0 break-all text-muted">{model.technicalName}</dd></div>
        <div><dt class="font-extrabold">Internal status</dt><dd class="m-0 text-muted">{model.internalStatus}</dd></div>
        <div><dt class="font-extrabold">Stored Telegram credential</dt><dd class="m-0 text-muted">{model.hasStoredCredential ? 'present' : 'missing'}</dd></div>
        <div><dt class="font-extrabold">Last heartbeat at</dt><dd class="m-0 break-all text-muted">{model.lastHeartbeatAt}</dd></div>
        <div><dt class="font-extrabold">Live listener started at</dt><dd class="m-0 break-all text-muted">{model.liveListenerStartedAt}</dd></div>
        <div><dt class="font-extrabold">Rate-limit hold until</dt><dd class="m-0 break-all text-muted">{model.floodWaitUntil}</dd></div>
        <div><dt class="font-extrabold">Quarantined at</dt><dd class="m-0 break-all text-muted">{model.quarantinedAt}</dd></div>
        <div><dt class="font-extrabold">Last error category</dt><dd class="m-0 break-words text-muted">{model.errorClass ?? 'None'}</dd></div>
        <div><dt class="font-extrabold">Provider details</dt><dd class="m-0 break-words text-muted">{model.providerDetailsHidden ? 'Provider details are hidden.' : 'None'}</dd></div>
      </dl>
    </AdvancedSection>

    <AdvancedSection title="Advanced settings" description="Adjust crawler policy or repair account metadata only when routine connection and validation are not enough.">
      <form method="POST" action="?/updateSession" class="grid gap-3">
        <input type="hidden" name="session_id" value={account.id} />
        <div class="grid gap-2 md:grid-cols-3">
          <FormRow label="Account display name"><Input name="display_name" value={model.displayName} required /></FormRow>
          <FormRow label="Internal status">
            <Select name="status">
              {#each sessionStatuses as status}
                <option value={status} selected={status === account.status}>{status}</option>
              {/each}
            </Select>
          </FormRow>
          <FormRow label="Maximum requests per second"><Input name="max_requests_per_second" type="number" min="0.1" step="0.1" value={account.max_requests_per_second} required /></FormRow>
        </div>
        <div class="grid gap-2 md:grid-cols-3">
          <FormRow label="Rate-limit hold until"><Input name="flood_wait_until" value={model.floodWaitUntil === 'Not recorded' ? '' : model.floodWaitUntil} placeholder="ISO datetime or blank" /></FormRow>
          <FormRow label="Last error category"><Input name="last_error_class" value={model.errorClass ?? ''} /></FormRow>
          <FormRow label="Audit note"><Input name="note" placeholder="Optional operator context" /></FormRow>
        </div>
        <div class="grid gap-2 md:grid-cols-3">
          <label class="inline-flex items-center gap-2 text-chiptext"><input name="enabled" type="checkbox" checked={account.enabled} /> Enabled</label>
          <label class="inline-flex items-center gap-2 text-chiptext"><input name="catchup_enabled" type="checkbox" checked={account.catchup_enabled} /> Catch up</label>
          <label class="inline-flex items-center gap-2 text-chiptext"><input name="live_enabled" type="checkbox" checked={account.live_enabled} /> Watch new posts</label>
          <label class="inline-flex items-center gap-2 text-chiptext"><input name="engagement_enabled" type="checkbox" checked={account.engagement_enabled} /> Track engagement</label>
          <label class="inline-flex items-center gap-2 text-chiptext"><input name="clear_error" type="checkbox" /> Clear saved error</label>
        </div>
        <Button type="submit">Save advanced settings</Button>
      </form>
    </AdvancedSection>

    <AdvancedSection title="Disconnect account" description={sourceImpactCopy} danger>
      <form method="POST" action="?/deleteSession" class="grid gap-3">
        <input type="hidden" name="session_id" value={account.id} />
        <FormRow label="Type DISCONNECT to permanently delete this account" hint="This permanently deletes the account record and disables ingestion for every assigned source.">
          <Input name="confirmation" autocomplete="off" placeholder="DISCONNECT" required />
        </FormRow>
        <FormRow label="Operator note (optional)"><Textarea name="note" rows={2} /></FormRow>
        <Button type="submit" variant="danger">Disconnect account</Button>
      </form>
    </AdvancedSection>
  </div>
</article>
