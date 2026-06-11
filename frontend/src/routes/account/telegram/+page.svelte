<script lang="ts">
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

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
  const telegramConnected = $derived(data.session?.linked_providers.telegram_linked === true);
  const isFull = $derived(data.session?.user.account_type === 'full');
</script>

<section class="telegram-panel" aria-labelledby="telegram-title">
  <div>
    <p class="pill">Continuous account linking</p>
    <h1 id="telegram-title">Connect Telegram to keep saves and favorites.</h1>
    <p class="muted">
      We keep your current browser session as a guest until Telegram confirms the link. If Telegram already belongs to a
      full profile, the backend merges this guest session into that profile and this browser gets a replacement cookie on
      refresh.
    </p>
  </div>

  <div class="telegram-card">
    {#if telegramConnected}
      <h2>Telegram is connected</h2>
      <p class="muted">Your saves and favorites are attached to this full profile.</p>
      <a class="button-link" href={data.returnTo}>Continue browsing</a>
    {:else if isFull}
      <h2>Full profile active</h2>
      <p class="muted">This profile is already full. Telegram linking for full profiles is not exposed in this web slice.</p>
      <a class="button-link secondary" href={data.returnTo}>Back to memes</a>
    {:else}
      <h2>Start from this guest session</h2>
      <p class="muted">Use the Telegram bot link, finish there, then come back and refresh this page.</p>

      <form method="POST" action="?/start" class="stacked-form">
        <button type="submit">Start Telegram link</button>
      </form>

      {#if link}
        <div class="handoff-box" role="status">
          <p class="caption">Telegram handoff is ready.</p>
          <a class="button-link" href={link.deep_link_url} target="_blank" rel="noreferrer">Open Telegram</a>
          <p class="muted">This link expires in about {Math.ceil(link.expires_in_seconds / 60)} minutes.</p>
        </div>
      {/if}

      <form method="POST" action="?/refresh" class="stacked-form">
        <button type="submit" class="secondary-button">I finished in Telegram, refresh my session</button>
      </form>

      {#if message}
        <p class="notice" role="status">{message}</p>
      {/if}
    {/if}
  </div>
</section>
