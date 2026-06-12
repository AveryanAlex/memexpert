<script lang="ts">
  import '../app.css';
  import { accountBenefitText, accountStatusLabel, connectedProviderLabels } from '$lib/account/view-model';
  import type { Snippet } from 'svelte';
  import type { LayoutData } from './$types';

  let { data, children }: { data: LayoutData; children: Snippet } = $props();

  const providerLabels = $derived(connectedProviderLabels(data.session?.linked_providers ?? null));
  const canConnectTelegram = $derived(
    data.session?.user.account_type === 'guest' && !data.session.linked_providers.telegram_linked
  );
</script>

<div class="shell">
  <header class="site-header">
    <div class="brand-block">
      <a class="brand" href="/">MemeXpert</a>
      <a class="pill" href="/trends">Trends</a>
      <span class="pill">Public catalog MVP</span>
    </div>
    <section class="account-chip" aria-label="Account state">
      <div>
        <p class="account-title">{accountStatusLabel(data.session)}</p>
        <p class="account-copy">{accountBenefitText(data.session)}</p>
        {#if providerLabels.length > 0}
          <p class="account-copy">Connected: {providerLabels.join(', ')}</p>
        {:else if data.session}
          <p class="account-copy">Connected: none yet</p>
        {/if}
      </div>
      <div class="account-actions">
        {#if canConnectTelegram}
          <a class="button-link compact" href="/account/telegram">Connect Telegram</a>
        {:else if data.session?.linked_providers.telegram_linked}
          <span class="pill success">Telegram connected</span>
        {:else if data.sessionError}
          <a class="button-link compact secondary" href="/account/telegram">Refresh account</a>
        {/if}
        <span class="stub-pill" aria-label="Google and email linking not available yet">Google/email later</span>
      </div>
    </section>
  </header>

  {@render children()}
</div>
