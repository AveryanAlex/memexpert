<script lang="ts">
  import { readAuthState } from '$lib/auth-state';
  import InfiniteMemeFeed from '$lib/features/memes/InfiniteMemeFeed.svelte';
  import type { MemeFeedSource } from '$lib/features/memes/infinite-feed';
  import MemeOfTheDayPanel from '$lib/features/memes/MemeOfTheDayPanel.svelte';
  import { ActionLink } from '$lib/ui';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  const authState = readAuthState(() => ({ session: data.session ?? null, sessionError: data.sessionError }));
  const session = $derived($authState.session);

  const feedSource = $derived(toMemeFeedSource(data.feedSource));
  const isHomeFeed = $derived(feedSource === 'home' && !data.query.trim());

  function toMemeFeedSource(value: PageData['feedSource']): MemeFeedSource {
    return value === 'home' ? 'home' : 'catalog';
  }
</script>

<section class="mb-4 flex flex-wrap items-end justify-between gap-3" aria-labelledby="discover-heading">
  <div>
    <h1 id="discover-heading" class="m-0 text-2xl font-black tracking-[-0.05em] text-ink sm:text-3xl">Discover</h1>
    <p class="m-0 text-sm text-muted">Fresh memes, ready to send.</p>
  </div>
  <nav class="flex flex-wrap gap-2" aria-label="Discover navigation">
    <ActionLink size="compact" variant="secondary" href="/search">Search</ActionLink>
    <ActionLink size="compact" variant="ghost" href="/trends">Trends</ActionLink>
  </nav>
</section>

<MemeOfTheDayPanel memeOfTheDay={data.memeOfTheDay} initialError={data.memeOfTheDayErrorMessage} showAccessMarkers={Boolean(session)} />

<nav class="mb-5 flex gap-2 overflow-x-auto pb-1" aria-label="Popular topics">
  <a class="shrink-0 rounded-full border border-line bg-paper px-3 py-1.5 text-sm font-semibold text-ink no-underline hover:bg-soft" href="/search?q=reaction">Reactions</a>
  <a class="shrink-0 rounded-full border border-line bg-paper px-3 py-1.5 text-sm font-semibold text-ink no-underline hover:bg-soft" href="/search?q=work">Work</a>
  <a class="shrink-0 rounded-full border border-line bg-paper px-3 py-1.5 text-sm font-semibold text-ink no-underline hover:bg-soft" href="/search?q=animals">Animals</a>
</nav>

<InfiniteMemeFeed
  initialPage={data.page}
  filters={{ query: data.query }}
  source={feedSource}
  initialError={data.errorMessage}
  emptyTitle={isHomeFeed ? 'No home feed memes yet' : 'No memes found'}
  emptyMessage={isHomeFeed ? 'Try Search or check back soon.' : 'Try a shorter phrase, a different synonym, or clear the search box to browse.'}
  bulk={{ enabled: false }}
  showAccessMarkers={Boolean(session)}
>
  {#snippet summary()}
    {#if data.query}
      <p class="m-0 text-muted">
        Results for “{data.query}”
      </p>
    {:else}
      <p class="m-0 font-semibold text-ink">Discover more</p>
    {/if}
  {/snippet}
  {#snippet emptyAction()}
    {#if isHomeFeed}
      <ActionLink href="/search">Search memes</ActionLink>
    {/if}
  {/snippet}
</InfiniteMemeFeed>
