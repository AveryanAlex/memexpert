<script lang="ts">
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<section class="telegram-panel" aria-labelledby="invite-title">
  <div>
    <a class="eyebrow-link" href="/">Back to catalog</a>
    <h1 id="invite-title">Join a meme collection</h1>
    <p class="muted">This invite can add your full MemeXpert account to a shared collection.</p>
  </div>

  <form class="telegram-card stacked-form" method="POST">
    <h2>Collection invite</h2>
    <p class="muted">Invite token ending in {data.token.slice(-6)}</p>
    {#if form?.errorMessage}
      <p class="notice" role="alert">{form.errorMessage}</p>
      <p class="muted">Guests need to connect Telegram before joining shared custom collections.</p>
      <a class="button-link compact secondary" href="/account/telegram">Connect Telegram</a>
    {/if}
    <button type="submit">Join collection</button>
  </form>
</section>
