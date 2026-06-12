<script lang="ts">
  import '../app.css';
  import { accountBenefitText, accountStatusLabel, connectedProviderLabels } from '$lib/account/view-model';
  import { ActionLink, Badge, PageShell } from '$lib/ui';
  import TooltipProvider from '$lib/ui/tooltip/Provider.svelte';
  import type { Snippet } from 'svelte';
  import type { LayoutData } from './$types';

  let { data, children }: { data: LayoutData; children: Snippet } = $props();

  const providerLabels = $derived(connectedProviderLabels(data.session?.linked_providers ?? null));
  const canConnectTelegram = $derived(
    data.session?.user.account_type === 'guest' && !data.session.linked_providers.telegram_linked
  );
</script>

<TooltipProvider delayDuration={500}>
  <PageShell>
    <header class="mb-9 flex flex-col items-start justify-between gap-4 md:flex-row md:items-center">
      <div class="flex flex-wrap items-center gap-3">
        <a class="text-[clamp(1.6rem,5vw,2.45rem)] font-black tracking-[-0.05em] no-underline" href="/">MemeXpert</a>
        <a class="rounded-full border border-line px-3 py-2 text-sm text-muted no-underline" href="/trends">Trends</a>
        <Badge>Public catalog MVP</Badge>
      </div>

      <section class="flex w-full max-w-xl flex-col justify-between gap-3 rounded-3xl border border-line bg-paper p-3 shadow-warm md:flex-row md:items-center" aria-label="Account state">
        <div>
          <p class="m-0 font-black">{accountStatusLabel(data.session)}</p>
          <p class="m-0 text-sm text-muted">{accountBenefitText(data.session)}</p>
          {#if providerLabels.length > 0}
            <p class="m-0 text-sm text-muted">Connected: {providerLabels.join(', ')}</p>
          {:else if data.session}
            <p class="m-0 text-sm text-muted">Connected: none yet</p>
          {/if}
        </div>

        <div class="flex flex-wrap gap-2 md:justify-end">
          {#if canConnectTelegram}
            <ActionLink size="compact" href="/account/telegram">Connect Telegram</ActionLink>
          {:else if data.session?.linked_providers.telegram_linked}
            <Badge tone="success">Telegram connected</Badge>
          {:else if data.sessionError}
            <ActionLink size="compact" variant="secondary" href="/account/telegram">Refresh account</ActionLink>
          {/if}
          <span class="whitespace-nowrap rounded-full bg-soft px-3 py-2 text-xs text-muted" aria-label="Google and email linking not available yet">Google/email later</span>
        </div>
      </section>
    </header>

    {@render children()}
  </PageShell>
</TooltipProvider>
