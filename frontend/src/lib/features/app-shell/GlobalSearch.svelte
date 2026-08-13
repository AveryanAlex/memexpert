<script lang="ts">
  import { Search } from '@lucide/svelte';
  import { ActionLink, Button, Input } from '$lib/ui';

  let { memeCount = null }: { memeCount?: number | null } = $props();

  const formId = 'global-search-form';
  let query = $state('');
  const placeholder = $derived(
    memeCount === null
      ? 'Search memes, reactions, templates…'
      : `Search among ${memeCount.toLocaleString('en-US')} memes, reactions, templates…`
  );
</script>

<form id={formId} class="hidden min-w-0 flex-1 items-center gap-2 rounded-[16px] border border-line bg-paper/95 px-3 py-1.5 shadow-overlay backdrop-blur md:flex" method="GET" action="/search" role="search">
  <label class="sr-only" for="global-search-q">Search memes</label>
  <Search class="size-4 shrink-0 text-muted" aria-hidden="true" />
  <Input id="global-search-q" class="h-9 min-w-0 flex-1 border-0 bg-transparent px-0 py-0 text-sm placeholder:text-muted focus-visible:outline-offset-0" name="q" type="search" bind:value={query} {placeholder} />
  <Button class="shrink-0" size="compact" type="submit">Search</Button>
</form>

<ActionLink class="shrink-0 md:hidden" href="/search" variant="secondary" size="compact">
  <Search class="size-4" aria-hidden="true" />
  <span>Search</span>
</ActionLink>
