<script lang="ts">
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import type { AdminTelegramSessionRead } from '$lib/api/types';
  import { EmptyState } from '$lib/ui';
  import TelegramAccountCard from './TelegramAccountCard.svelte';
  import { loginStateForAccount, type TelegramPasswordLoginStep } from './view-model';

  let {
    accounts,
    loadedAt,
    form,
    qrPasswordStep,
    onStartQrLogin
  }: {
    accounts: AdminTelegramSessionRead[];
    loadedAt: string;
    form: unknown;
    qrPasswordStep: TelegramPasswordLoginStep | null;
    onStartQrLogin?: (event: SubmitEvent) => void;
  } = $props();
</script>

<AdminPanel title="Telegram accounts">
  {#if accounts.length === 0}
    <EmptyState title="No Telegram accounts" message="Connect an account with QR, or use the phone alternative if needed." />
  {:else}
    <div class="grid gap-4">
      {#each accounts as account (account.id)}
        <TelegramAccountCard {account} {loadedAt} loginState={loginStateForAccount(form, account.id, qrPasswordStep)} {onStartQrLogin} />
      {/each}
    </div>
  {/if}
</AdminPanel>
