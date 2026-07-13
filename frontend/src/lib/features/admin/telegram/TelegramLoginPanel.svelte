<script lang="ts">
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { Button, FormRow, Input, Notice } from '$lib/ui';
  import { safePhoneHint, type TelegramLoginState } from './view-model';

  let {
    loginState = null,
    onStartQrLogin
  }: {
    loginState?: TelegramLoginState | null;
    onStartQrLogin?: (event: SubmitEvent) => void;
  } = $props();

  const phoneHint = $derived(loginState?.kind === 'phone_code' ? safePhoneHint(loginState.phoneHint) : null);
  const passwordPhoneHint = $derived(loginState?.kind === 'password' ? safePhoneHint(loginState.phoneHint) : null);
</script>

<AdminPanel title="Connect a Telegram account">
  <div class="grid gap-4">
    {#if loginState?.kind === 'phone_code'}
      <form method="POST" action="?/completePhoneCodeLogin" class="grid max-w-xl gap-3 rounded-2xl border border-line bg-soft/40 p-4">
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
        <div class="flex flex-wrap gap-2">
          <Button type="submit">Continue</Button>
          <Button type="submit" variant="ghost" formaction="?/cancelLoginAttempt" formnovalidate>Cancel sign-in</Button>
        </div>
      </form>
    {:else if loginState?.kind === 'password'}
      <form method="POST" action="?/completePhonePasswordLogin" class="grid max-w-xl gap-3 rounded-2xl border border-line bg-soft/40 p-4">
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
        <div class="flex flex-wrap gap-2">
          <Button type="submit">Finish connecting</Button>
          <Button type="submit" variant="ghost" formaction="?/cancelLoginAttempt" formnovalidate>Cancel sign-in</Button>
        </div>
      </form>
    {:else}
      {#if loginState?.kind === 'login_error'}
        <Notice tone="danger" role="alert"><strong>Sign-in did not finish.</strong> {loginState.message} Restart the connection below.</Notice>
      {/if}
      <div class="grid gap-2">
        <p class="m-0 text-sm text-muted">Connect an account to fetch Telegram sources. QR is the quickest option and does not require entering a phone number here.</p>
        <form method="POST" action="?/startQrLogin" onsubmit={onStartQrLogin} class="flex flex-wrap gap-2">
          <Button type="submit">{loginState?.kind === 'login_error' ? 'Restart with QR' : 'Connect with QR'}</Button>
        </form>
      </div>

      <AdvancedSection title="Use phone instead" description="Enter a phone number only when QR sign-in is not available. Telegram will ask for its verification code next.">
        <form method="POST" action="?/startPhoneLogin" class="grid max-w-xl gap-3">
          <FormRow label="Phone number">
            <Input name="phone_number" autocomplete="tel" inputmode="tel" placeholder="Enter full phone number" required />
          </FormRow>
          <Button type="submit" variant="secondary">Continue with phone</Button>
        </form>
      </AdvancedSection>
    {/if}
  </div>
</AdminPanel>
