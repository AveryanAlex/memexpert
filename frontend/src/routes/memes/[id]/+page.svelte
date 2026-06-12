<script lang="ts">
  import { page } from '$app/state';
  import MemeActionMenu from '$lib/features/memes/MemeActionMenu.svelte';
  import MemeMedia from '$lib/features/memes/MemeMedia.svelte';
  import TrendSparkline from '$lib/features/trends/TrendSparkline.svelte';
  import TrendSummary from '$lib/features/trends/TrendSummary.svelte';
  import { ActionLink, Badge, Button, Card, EmptyState, Notice } from '$lib/ui';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

  const returnTo = $derived(page.url.pathname);
</script>

{#if data.meme}
  <article class="grid gap-6 rounded-[28px] border border-line bg-paper p-6 md:grid-cols-[minmax(260px,0.9fr)_minmax(0,1fr)]">
    <MemeMedia meme={data.meme} detail />
    <div>
      <Badge>{data.meme.language} · {data.meme.like_count} likes</Badge>
      <h1 class="mb-4 mt-3 text-[clamp(2rem,7vw,4.5rem)] font-black leading-[0.95] tracking-[-0.06em]">{data.meme.seo_title || data.meme.caption || 'Meme detail'}</h1>
      <MemeActionMenu meme={data.meme} showPrimary />
      {#if data.meme.seo_description}
        <p>{data.meme.seo_description}</p>
      {:else if data.meme.caption}
        <p>{data.meme.caption}</p>
      {/if}
      {#if data.meme.ocr_text}
        <p class="text-muted">Detected text: {data.meme.ocr_text}</p>
      {/if}
      {#if data.meme.seo_body_text}
        <p>{data.meme.seo_body_text}</p>
      {/if}
      <div class="flex flex-wrap gap-2">
        <Badge>{data.meme.files.length || (data.meme.primary_file ? 1 : 0)} files</Badge>
        <Badge>score {data.meme.popularity_score.toFixed(1)}</Badge>
        {#if data.meme.primary_file?.mime_type}
          <Badge>{data.meme.primary_file.mime_type}</Badge>
        {/if}
      </div>
      {#if data.meme.tags.length > 0}
        <div class="mt-2 flex flex-wrap gap-2" aria-label="Tags">
          {#each data.meme.tags as tag}
            <Badge><a class="no-underline" href={`/tags/${tag}`}>#{tag}</a></Badge>
          {/each}
        </div>
      {/if}
      <Card class="mt-5 grid gap-3 shadow-none" aria-label="Popularity trend">
        <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Popularity trend</h2>
        <TrendSummary trend={data.popularity?.trend ?? null} />
        {#if data.popularity}
          <TrendSparkline points={data.popularity.sparkline} />
        {:else}
          <p class="m-0 text-muted">Popularity analytics are not available for this meme yet.</p>
        {/if}
      </Card>
      <div class="mt-6 flex flex-wrap gap-2">
        <form method="POST" action="?/favorite">
          <input type="hidden" name="memeId" value={data.meme.id} />
          <Button type="submit">Save to favorites</Button>
        </form>
        <ActionLink variant="secondary" href="/">Back to search</ActionLink>
      </div>
      {#if form?.message}
        <Notice>{form.message}</Notice>
      {/if}
      {#if form?.status === 'saved' && form.showConnectTelegram}
        <Card class="mt-4 grid gap-2 border-success-line bg-success-surface shadow-none" aria-label="Keep favorites">
          <p class="m-0 text-lg font-extrabold leading-tight">Keep this save beyond this browser.</p>
          <ActionLink href={`/account/telegram?returnTo=${encodeURIComponent(returnTo)}`}>
            Connect Telegram to keep saves/favorites
          </ActionLink>
        </Card>
      {/if}
    </div>
  </article>
{:else}
  <EmptyState title="Meme unavailable" message={data.unavailableMessage}>
    <ActionLink href="/">Search public memes</ActionLink>
  </EmptyState>
{/if}
