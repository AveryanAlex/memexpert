<script lang="ts">
  import { readAuthState } from '$lib/auth-state';
  import { ActionLink, Badge, Button, Card, Notice } from '$lib/ui';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

  const authState = readAuthState(() => ({ session: data.session ?? null, sessionError: data.sessionError }));
  const session = $derived($authState.session);

  const status = $derived(form?.status ?? null);
  const link = $derived.by(() => {
    if (form && status === 'started' && 'link' in form) {
      return form.link;
    }
    return null;
  });
  const message = $derived.by(() => {
    if (form && 'message' in form) {
      return form.message;
    }
    return null;
  });
  const telegramConnected = $derived(session?.linked_providers.telegram_linked === true);
  const isFull = $derived(session?.user.account_type === 'full');
</script>

<section class="grid items-start gap-6 md:grid-cols-[minmax(0,0.95fr)_minmax(320px,0.7fr)]" aria-labelledby="telegram-title">
  <div>
    <Badge>Continuous account linking</Badge>
    <h1 id="telegram-title" class="my-4 text-[clamp(2.2rem,7vw,5rem)] font-black leading-[0.94] tracking-[-0.06em]">Connect Telegram to keep saves and favorites.</h1>
    <p class="text-muted">
      We keep your current browser session as a guest until Telegram confirms the link. If Telegram already belongs to a
      full profile, the backend merges this guest session into that profile and this browser gets a replacement cookie on
      refresh.
    </p>
  </div>

  <Card class="grid gap-3 shadow-none">
    {#if telegramConnected}
      <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Telegram is connected</h2>
      <p class="m-0 text-muted">Your saves and favorites are attached to this full profile.</p>
      <ActionLink href={data.returnTo}>Continue browsing</ActionLink>
    {:else if isFull}
      <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Full profile active</h2>
      <p class="m-0 text-muted">This profile is already full. Telegram linking for full profiles is not exposed in this web slice.</p>
      <ActionLink variant="secondary" href={data.returnTo}>Back to memes</ActionLink>
    {:else}
      <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Start from this guest session</h2>
      <p class="m-0 text-muted">Use the Telegram bot link, finish there, then return to MemeXpert. Your next page load updates this browser automatically.</p>

      <form method="POST" action="?/start" class="my-3 grid gap-2">
        <Button type="submit">Start Telegram link</Button>
      </form>

      {#if link}
        <div class="grid gap-2 rounded-[20px] border border-dashed border-[#c7b9a7] bg-[#f8efe1] p-4" role="status">
          <p class="m-0 text-lg font-extrabold leading-tight">Telegram handoff is ready.</p>
          <ActionLink href={link.deep_link_url} target="_blank" rel="noreferrer">Open Telegram</ActionLink>
          <p class="m-0 text-muted">This link expires in about {Math.ceil(link.expires_in_seconds / 60)} minutes.</p>
        </div>
      {/if}

      {#if message}
        <Notice>{message}</Notice>
      {/if}
    {/if}
  </Card>
</section>
