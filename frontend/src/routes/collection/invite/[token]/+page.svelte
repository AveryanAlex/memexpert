<script lang="ts">
  import { ActionLink, Button, Card, Notice } from '$lib/ui';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<section class="grid items-start gap-6 md:grid-cols-[minmax(0,0.95fr)_minmax(320px,0.7fr)]" aria-labelledby="invite-title">
  <div>
    <a class="text-sm font-black text-muted underline decoration-2 underline-offset-4" href="/">Back to catalog</a>
    <h1 id="invite-title" class="my-4 text-[clamp(2.2rem,7vw,5rem)] font-black leading-[0.94] tracking-[-0.06em]">Join a meme collection</h1>
    <p class="text-muted">This invite can add your full MemeXpert account to a shared collection.</p>
  </div>

  <Card class="shadow-none">
  <form class="grid gap-3" method="POST">
    <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Collection invite</h2>
    <p class="m-0 text-muted">Invite token ending in {data.token.slice(-6)}</p>
    {#if form?.errorMessage}
      <Notice role="alert" tone="danger">{form.errorMessage}</Notice>
      <p class="m-0 text-muted">Guests need to connect Telegram before joining shared custom collections.</p>
      <ActionLink size="compact" variant="secondary" href="/account/telegram">Connect Telegram</ActionLink>
    {/if}
    <Button type="submit">Join collection</Button>
  </form>
  </Card>
</section>
