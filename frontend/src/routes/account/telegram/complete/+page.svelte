<script lang="ts">
  import { ActionLink, Badge, Card, Notice } from '$lib/ui';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();
</script>

<section class="grid items-start gap-6 md:grid-cols-[minmax(0,0.95fr)_minmax(320px,0.7fr)]" aria-labelledby="telegram-complete-title">
  <div>
    <Badge>Telegram return</Badge>
    <h1 id="telegram-complete-title" class="my-4 text-[clamp(2.2rem,7vw,5rem)] font-black leading-[0.94] tracking-[-0.06em]">Telegram link is not visible in this browser yet.</h1>
    <p class="text-muted">
      If Telegram opened the same browser that started the link, this page redirects automatically after the backend repairs the cookie. If you see this message, Telegram likely returned in a different browser or the link was not completed.
    </p>
  </div>

  <Card class="grid gap-3 shadow-none">
    <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Return to the original MemeXpert tab</h2>
    <p class="m-0 text-muted">
      The original tab has the guest cookie that can be repaired into your connected account cookie. If that tab is gone, start a new Telegram link from this browser.
    </p>
    {#if data.sessionError}
      <Notice>{data.sessionError}</Notice>
    {/if}
    {#if data.accountType === 'guest'}
      <Notice>This browser currently has a guest session, not the Telegram-connected profile.</Notice>
    {/if}
    <div class="flex flex-wrap gap-2">
      <ActionLink href="/account/telegram">Start a new Telegram link</ActionLink>
      <ActionLink variant="secondary" href={data.returnTo}>Continue browsing</ActionLink>
    </div>
  </Card>
</section>
