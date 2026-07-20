<script lang="ts">
  import { browser } from '$app/environment';
  import { uuidV7 } from '$lib/analytics/uuid-v7';
  import { recordMemeView } from '$lib/api/client';
  import { readAuthState } from '$lib/auth-state';
  import InfiniteMemeFeed from '$lib/features/memes/InfiniteMemeFeed.svelte';
  import MemeActionMenu from '$lib/features/memes/MemeActionMenu.svelte';
  import MemeSourcesAndActivity from '$lib/features/memes/MemeSourcesAndActivity.svelte';
  import MemeMedia from '$lib/features/memes/MemeMedia.svelte';
  import TrendSparkline from '$lib/features/trends/TrendSparkline.svelte';
  import { buildMemeDetailView } from '$lib/meme-detail-view';
  import { memeActionAttributionBody } from '$lib/memeActions';
  import { ActionLink, Badge, Card, EmptyState, Notice } from '$lib/ui';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

  const authState = readAuthState(() => ({ session: data.session ?? null, sessionError: data.sessionError }));
  const session = $derived($authState.session);

  const returnTo = $derived(data.meme ? `/memes/${data.meme.seo_page_slug ?? data.meme.id}` : '/');
  const detail = $derived(data.meme ? buildMemeDetailView(data.meme) : null);
  const popularityTrend = $derived(data.popularity?.trend ?? null);
  const popularityPoints = $derived(data.popularity?.sparkline ?? []);
  let showGuestFavoritePrompt = $state(false);
  let recordedViewMemeId = $state<string | null>(null);

  $effect(() => {
    const memeId = data.meme?.id;
    if (!browser || !memeId || recordedViewMemeId === memeId) return;

    recordedViewMemeId = memeId;
    void recordMemeView({
      fetch,
      memeId,
      body: memeActionAttributionBody(data.attribution, uuidV7()),
      keepalive: true
    }).catch((error) => console.warn('Meme detail telemetry failed.', { action: 'view', memeId, error }));
  });

  function tagHref(tag: string): string {
    return `/tags/${encodeURIComponent(tag)}`;
  }

  function handleFavoriteChange(favorited: boolean) {
    showGuestFavoritePrompt = favorited && session?.user.account_type !== 'full';
  }
</script>

<svelte:head>
  {#if detail}
    <title>{detail.title} | MemeXpert</title>
    <meta name="description" content={detail.metaDescription ?? 'Public MemeXpert meme detail page for sharing, saving, and discovery.'} />
    <meta property="og:title" content={detail.title} />
    <meta property="og:description" content={detail.metaDescription ?? 'Public MemeXpert meme detail page.'} />
  {/if}
</svelte:head>

{#if data.meme && detail}
  <article class="grid gap-5 lg:grid-cols-[minmax(0,1.15fr)_minmax(19rem,0.85fr)] lg:items-start lg:gap-8">
    <div class="min-w-0">
      <MemeMedia meme={data.meme} detail />
    </div>

    <div class="grid content-start gap-5 lg:sticky lg:top-6">
      <div class="order-2 grid gap-5 lg:order-1">
        <header>
          {#if data.meme.is_nsfw}
            <Badge class="mb-3">Sensitive content</Badge>
          {/if}
          <h1 class="m-0 text-[clamp(2.25rem,7vw,4.5rem)] font-black leading-[0.9] tracking-[-0.07em]">{detail.title}</h1>
          {#if detail.leadDescription}
            <p class="mb-0 mt-4 max-w-3xl text-lg leading-relaxed text-muted">{detail.leadDescription}</p>
          {/if}
        </header>

        {#if data.meme.tags.length > 0}
          <section class="grid gap-3" aria-labelledby="meme-tags-title">
            <h2 id="meme-tags-title" class="m-0 text-lg font-extrabold">Tags</h2>
            <div class="flex flex-wrap gap-2">
              {#each data.meme.tags as tag}
                <Badge><a class="no-underline" href={tagHref(tag)}>#{tag}</a></Badge>
              {/each}
            </div>
          </section>
        {/if}
      </div>

      <div class="order-1 grid gap-3 lg:order-2" aria-label="Meme actions">
        <MemeActionMenu meme={data.meme} attribution={data.attribution} surface="detail" onFavoriteChange={handleFavoriteChange} />

        {#if form?.message}
          <Notice>{form.message}</Notice>
        {/if}
        {#if showGuestFavoritePrompt || (form?.status === 'saved' && form.showConnectTelegram)}
          <Card class="grid gap-2 border-success-line bg-success-surface shadow-none" aria-label="Keep favorites">
            <p class="m-0 text-lg font-extrabold leading-tight">Keep this save beyond this browser.</p>
            <ActionLink href={`/account/telegram?returnTo=${encodeURIComponent(returnTo)}`}>
              Connect Telegram to keep saves/favorites
            </ActionLink>
          </Card>
        {/if}
      </div>

      <details class="order-3 rounded-xl border border-line bg-paper">
        <summary class="cursor-pointer px-4 py-3 font-extrabold text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent">
          About this meme
        </summary>
        <div class="grid gap-5 border-t border-line px-4 py-5">
          {#if detail.bodyText}
            <section class="grid gap-2" aria-labelledby="meme-context-title">
              <h2 id="meme-context-title" class="m-0 text-lg font-extrabold">Context</h2>
              <p class="m-0 leading-relaxed">{detail.bodyText}</p>
            </section>
          {/if}

          {#if detail.detectedText}
            <section class="grid gap-2" aria-labelledby="meme-text-title">
              <h2 id="meme-text-title" class="m-0 text-lg font-extrabold">Text detected in the image</h2>
              <p class="m-0 leading-relaxed text-muted">{detail.detectedText}</p>
            </section>
          {/if}

          {#if popularityTrend || popularityPoints.length >= 2}
            <section class="grid gap-3" aria-labelledby="meme-popularity-title">
              <h2 id="meme-popularity-title" class="m-0 text-lg font-extrabold">Popularity</h2>
              {#if popularityTrend}
                <div class="flex flex-wrap gap-2" aria-label="Popularity summary">
                  <Badge>{popularityTrend.recent.views} views</Badge>
                  <Badge>{popularityTrend.recent.sends} sends</Badge>
                  <Badge>{popularityTrend.recent.likes} likes</Badge>
                  <Badge>{popularityTrend.recent.saves} saves</Badge>
                </div>
              {/if}
              {#if popularityPoints.length >= 2}
                <TrendSparkline points={popularityPoints} />
              {/if}
            </section>
          {/if}
        </div>
      </details>
    </div>
  </article>

  <MemeSourcesAndActivity
    sourcePage={data.sourcePage}
    sourceError={data.sourceError}
    analytics={data.analytics}
    analyticsError={data.analyticsError}
    insightsParams={data.insightsParams}
    pathname={data.insightsUrl.pathname}
    search={data.insightsUrl.search}
  />

  <section class="mt-8 grid gap-4 border-t border-line pt-6" aria-labelledby="similar-memes-title">
    <div>
      <h2 id="similar-memes-title" class="m-0 text-3xl font-black tracking-[-0.05em]">Similar memes</h2>
      <p class="m-0 max-w-3xl text-muted">Keep exploring with more memes from the catalog.</p>
    </div>

    <InfiniteMemeFeed
      initialPage={data.similarPage}
      filters={{ query: '' }}
      initialError={data.similarErrorMessage}
      retainInitialState={data.retainSimilarPage}
      source="similar"
      sourceMemeId={data.meme.id}
      label="Similar memes"
      emptyTitle="No similar memes yet"
      emptyMessage="More memes will appear here as the catalog grows."
      bulk={{ enabled: false }}
      showAccessMarkers={Boolean(session)}
    />
  </section>
{:else}
  <EmptyState title="Meme unavailable" message={data.unavailableMessage}>
    <ActionLink href="/">Search public memes</ActionLink>
  </EmptyState>
{/if}
