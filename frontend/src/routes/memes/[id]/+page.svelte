<script lang="ts">
  import { page } from '$app/state';
  import { recordMemeDownload } from '$lib/api/client';
  import MemeActionMenu from '$lib/features/memes/MemeActionMenu.svelte';
  import MemeGrid from '$lib/features/memes/MemeGrid.svelte';
  import MemeMedia from '$lib/features/memes/MemeMedia.svelte';
  import TrendSparkline from '$lib/features/trends/TrendSparkline.svelte';
  import TrendSummary from '$lib/features/trends/TrendSummary.svelte';
  import { buildMemeDetailView, buildRelatedDiscovery, formatFileSize } from '$lib/meme-detail-view';
  import { memeActionAttributionBody } from '$lib/memeActions';
  import { ActionLink, Badge, Card, EmptyState, Notice } from '$lib/ui';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

  const returnTo = $derived(page.url.pathname);
  const detail = $derived(data.meme ? buildMemeDetailView(data.meme) : null);
  const related = $derived(data.meme ? buildRelatedDiscovery(data.meme, data.relatedSource) : null);

  function tagHref(tag: string): string {
    return `/tags/${encodeURIComponent(tag)}`;
  }

  function recordDirectDownload() {
    if (!data.meme) return;
    void recordMemeDownload({ fetch, memeId: data.meme.id, body: memeActionAttributionBody(data.attribution) }).catch(() => undefined);
  }
</script>

<svelte:head>
  {#if detail}
    <title>{detail.title} | MemeXpert</title>
    <meta name="description" content={detail.description ?? 'Public MemeXpert meme detail page for sharing, saving, and discovery.'} />
    <meta property="og:title" content={detail.title} />
    <meta property="og:description" content={detail.description ?? 'Public MemeXpert meme detail page.'} />
  {/if}
</svelte:head>

{#if data.meme && detail && related}
  <article class="grid gap-6 rounded-[32px] border border-line bg-paper p-4 shadow-warm-lg md:grid-cols-[minmax(280px,0.95fr)_minmax(0,1fr)] md:p-6">
    <div class="grid content-start gap-4">
      <MemeMedia meme={data.meme} detail />

      <Card class="grid gap-3 shadow-none" aria-label="Media and file info">
        <div>
          <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Media and file info</h2>
          <p class="m-0 text-muted">Only fields exposed by the public meme detail API are shown.</p>
        </div>

        <div class="flex flex-wrap gap-2">
          {#each detail.mediaFacts as fact}
            <Badge>{fact}</Badge>
          {/each}
          <Badge>score {detail.scoreLabel}</Badge>
          {#each detail.primaryFileFacts as fact}
            <Badge>{fact}</Badge>
          {/each}
        </div>

        {#if data.meme.files.length > 0}
          <div class="grid gap-2" aria-label="Files">
            {#each data.meme.files as file, index (file.id)}
              <div class="rounded-[18px] border border-line bg-soft p-3 text-sm">
                <p class="m-0 font-extrabold">File {index + 1}</p>
                <p class="m-0 text-muted">
                  {file.mime_type ?? data.meme.media_type}
                  {#if file.width && file.height}
                    · {file.width}x{file.height}
                  {/if}
                  {#if formatFileSize(file.file_size_bytes)}
                    · {formatFileSize(file.file_size_bytes)}
                  {/if}
                </p>
              </div>
            {/each}
          </div>
        {:else if data.meme.primary_file}
          <p class="m-0 text-muted">Primary file metadata is available, but no additional file list was returned.</p>
        {:else}
          <p class="m-0 text-muted">No public file metadata is available for this meme yet.</p>
        {/if}

        {#if detail.downloadUrl}
          <ActionLink variant="secondary" size="compact" href={detail.downloadUrl} download onclick={recordDirectDownload}>Direct media download</ActionLink>
        {:else}
          <p class="m-0 text-muted">Download is unavailable until the catalog exposes a media download URL.</p>
        {/if}
      </Card>
    </div>

    <div class="grid content-start gap-5">
      <div>
        <div class="flex flex-wrap gap-2">
          <Badge>{data.meme.language}</Badge>
          <Badge>{data.meme.media_type}</Badge>
          <Badge>{data.meme.like_count} likes</Badge>
          {#if data.meme.is_nsfw}
            <Badge>NSFW</Badge>
          {/if}
        </div>
        <h1 class="mb-4 mt-3 text-[clamp(2.25rem,7vw,5rem)] font-black leading-[0.9] tracking-[-0.07em]">{detail.title}</h1>
        {#if detail.description}
          <p class="max-w-3xl text-lg leading-relaxed">{detail.description}</p>
        {:else}
          <p class="max-w-3xl text-lg leading-relaxed text-muted">No public caption or SEO description is available yet.</p>
        {/if}
      </div>

      <Card class="grid gap-3 shadow-none" aria-label="Meme actions">
        <div>
          <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Share, save, or report</h2>
          <p class="m-0 text-muted">Use the public action surface for likes, favorites, active saves, pins, sharing, downloads, and moderation reports.</p>
        </div>
        <MemeActionMenu meme={data.meme} attribution={data.attribution} showPrimary showSharing />
      </Card>

      {#if detail.bodyText || detail.detectedText}
        <Card class="grid gap-3 shadow-none" aria-label="Meme text">
          <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Text and context</h2>
          {#if detail.bodyText}
            <p class="m-0 leading-relaxed">{detail.bodyText}</p>
          {/if}
          {#if detail.detectedText}
            <p class="m-0 text-muted"><span class="font-extrabold text-ink">Detected text:</span> {detail.detectedText}</p>
          {/if}
        </Card>
      {/if}

      {#if data.meme.tags.length > 0}
        <Card class="grid gap-3 shadow-none" aria-label="Tags">
          <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Tags</h2>
          <div class="flex flex-wrap gap-2">
            {#each data.meme.tags as tag}
              <Badge><a class="no-underline" href={tagHref(tag)}>#{tag}</a></Badge>
            {/each}
          </div>
        </Card>
      {/if}

      <Card class="grid gap-3 shadow-none" aria-label="Popularity trend">
        <div>
          <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Public popularity</h2>
          <p class="m-0 text-muted">Aggregate public counts and snapshots only. Empty states mean no public analytics are available yet.</p>
        </div>
        <TrendSummary trend={data.popularity?.trend ?? null} />
        {#if data.popularity}
          <TrendSparkline points={data.popularity.sparkline} />
        {:else}
          <p class="m-0 text-muted">Popularity analytics are not available for this meme yet.</p>
        {/if}
      </Card>

      <div class="flex flex-wrap gap-2">
        <ActionLink variant="secondary" href="/">Back to search</ActionLink>
        <ActionLink variant="ghost" href="/trends">Browse public trends</ActionLink>
      </div>

      {#if form?.message}
        <Notice>{form.message}</Notice>
      {/if}
      {#if form?.status === 'saved' && form.showConnectTelegram}
        <Card class="grid gap-2 border-success-line bg-success-surface shadow-none" aria-label="Keep favorites">
          <p class="m-0 text-lg font-extrabold leading-tight">Keep this save beyond this browser.</p>
          <ActionLink href={`/account/telegram?returnTo=${encodeURIComponent(returnTo)}`}>
            Connect Telegram to keep saves/favorites
          </ActionLink>
        </Card>
      {/if}
    </div>
  </article>

  <Card class="mt-7 grid gap-4 shadow-none" aria-label="Related discovery">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="m-0 text-3xl font-black tracking-[-0.05em]">{related.heading}</h2>
        <p class="m-0 max-w-3xl text-muted">{related.description}</p>
      </div>
      <ActionLink variant="secondary" size="compact" href={related.href}>{related.linkLabel}</ActionLink>
    </div>

    {#if related.memes.length > 0}
      <MemeGrid memes={related.memes} attributions={related.attributions} label="Discovery memes" />
    {:else}
      <p class="m-0 text-muted">No additional public memes were returned for this discovery fallback.</p>
    {/if}
  </Card>
{:else}
  <EmptyState title="Meme unavailable" message={data.unavailableMessage}>
    <ActionLink href="/">Search public memes</ActionLink>
  </EmptyState>
{/if}
