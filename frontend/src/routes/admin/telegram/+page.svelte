<script lang="ts">
  import { ActionLink, Badge, Notice } from '$lib/ui';
  import TelegramAccountList from '$lib/features/admin/telegram/TelegramAccountList.svelte';
  import TelegramLoginPanel from '$lib/features/admin/telegram/TelegramLoginPanel.svelte';
  import TelegramQrLoginDialog from '$lib/features/admin/telegram/TelegramQrLoginDialog.svelte';
  import { loginError, loginStateForNewAccount, passwordLoginStep, safeOperatorMessage, type TelegramPasswordLoginStep } from '$lib/features/admin/telegram/view-model';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

  let qrDialog = $state<{ startExistingQrLogin: (event: SubmitEvent) => Promise<void> } | null>(null);
  let qrPasswordStep = $state<TelegramPasswordLoginStep | null>(null);
  const newAccountLoginState = $derived(loginStateForNewAccount(form, qrPasswordStep));

  $effect(() => {
    const passwordStep = passwordLoginStep(form);
    if (passwordStep) {
      qrPasswordStep = passwordStep;
      return;
    }
    if (loginError(form)) {
      qrPasswordStep = null;
      return;
    }
    if (form?.message && !form.error) qrPasswordStep = null;
  });

  function startExistingQrLogin(event: SubmitEvent): void {
    void qrDialog?.startExistingQrLogin(event);
  }

  function setQrPasswordStep(step: TelegramPasswordLoginStep): void {
    qrPasswordStep = step;
  }
</script>

<section class="grid gap-3">
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div>
      <Badge>Signed in as {data.adminUser.email || data.adminUser.id}</Badge>
      <h1 class="my-3 text-[clamp(2.4rem,8vw,5rem)] font-black leading-[0.9] tracking-[-0.075em]">Telegram accounts</h1>
      <p class="m-0 max-w-3xl text-muted">Connect Telegram accounts, see which ones need attention, and keep source fetching healthy.</p>
    </div>
    <div class="flex flex-wrap gap-2">
      <ActionLink href="/admin/sources" variant="secondary">Manage sources</ActionLink>
      <ActionLink href="/admin" variant="secondary">Back to admin</ActionLink>
    </div>
  </div>
</section>

{#if form?.message}
  <Notice tone={form.error ? 'danger' : 'success'} role={form.error ? 'alert' : 'status'}>{safeOperatorMessage(form.message)}</Notice>
{/if}

{#if data.loadError}
  <Notice role="alert" tone="danger">{safeOperatorMessage(data.loadError)}</Notice>
{/if}

<TelegramQrLoginDialog bind:this={qrDialog} {form} onPasswordRequired={setQrPasswordStep} />

<TelegramLoginPanel loginState={newAccountLoginState} onStartQrLogin={startExistingQrLogin} />
<TelegramAccountList accounts={data.telegramAdmin.sessions} loadedAt={data.loadedAt} {form} {qrPasswordStep} onStartQrLogin={startExistingQrLogin} />
