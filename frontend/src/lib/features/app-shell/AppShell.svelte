<script lang="ts">
  import { Bookmark, Compass, Search, Shield, UserCircle } from '@lucide/svelte';
  import type { Snippet } from 'svelte';
  import type { CurrentSessionRead } from '$lib/api/types';
  import { Button, PageShell } from '$lib/ui';
  import GlobalSearch from './GlobalSearch.svelte';
  import { ADMIN_NAV_ITEM, isNavItemActive, PRIMARY_NAV_ITEMS } from './navigation';

  let {
    session,
    sessionError,
    currentPath = '/',
    onLoginClick,
    children
  }: { session: CurrentSessionRead | null; sessionError: string | null; currentPath?: string; onLoginClick?: () => void; children?: Snippet } = $props();

  const navigationItems = $derived(session?.user.is_admin ? [...PRIMARY_NAV_ITEMS, ADMIN_NAV_ITEM] : PRIMARY_NAV_ITEMS);
  const desktopNavigationItems = $derived(navigationItems.filter((item) => item.href !== '/search' && item.href !== '/profile'));
  const accountItem = PRIMARY_NAV_ITEMS.find((item) => item.href === '/profile') ?? PRIMARY_NAV_ITEMS[PRIMARY_NAV_ITEMS.length - 1];
  const accountIsActive = $derived(isNavItemActive(accountItem, currentPath));
</script>

<div class="telegram-miniapp-shell min-h-screen bg-canvas text-ink">
  <header class="app-shell-header sticky top-0 z-30 border-b border-line bg-paper/95 backdrop-blur-xl">
    <div class="mx-auto flex w-[min(1280px,calc(100%_-_24px))] items-center gap-2 py-2.5 sm:gap-3">
      <a class="app-shell-brand shrink-0 text-lg font-black tracking-[-0.055em] no-underline" href="/">MemeXpert</a>
      <nav class="hidden shrink-0 items-center gap-1 md:flex" aria-label="Primary navigation">
        {#each desktopNavigationItems as item}
          {@const active = isNavItemActive(item, currentPath)}
          <a class={active ? 'rounded-[14px] bg-soft px-3 py-2 text-sm font-semibold text-accent no-underline' : 'rounded-[14px] px-3 py-2 text-sm font-semibold text-muted no-underline hover:bg-cream hover:text-ink'} href={item.href} aria-current={active ? 'page' : undefined}>{item.label}</a>
        {/each}
      </nav>
      <GlobalSearch />
      {#if session?.user.account_type === 'full'}
        <a class="app-shell-account hidden shrink-0 items-center gap-2 rounded-[14px] border border-line bg-paper px-3 py-2 text-sm font-semibold no-underline hover:bg-soft md:inline-flex" href="/profile" aria-current={accountIsActive ? 'page' : undefined}><UserCircle class="size-4" aria-hidden="true" /> <span>Account</span></a>
      {:else}
        <Button class="app-shell-sign-in hidden shrink-0 md:inline-flex" size="compact" type="button" onclick={() => onLoginClick?.()}>Sign in</Button>
      {/if}
    </div>
    {#if sessionError}<p class="mx-auto m-0 w-[min(1280px,calc(100%_-_24px))] pb-2 text-xs text-danger">{sessionError}</p>{/if}
  </header>

  <PageShell class="w-[min(1280px,calc(100%_-_24px))] pb-28 pt-6 md:pb-10">
    {#if children}{@render children()}{/if}
  </PageShell>

  <nav class="app-shell-mobile-nav fixed inset-x-0 bottom-0 z-30 grid grid-flow-col auto-cols-fr border-t border-line bg-paper/95 px-2 pb-[max(0.375rem,env(safe-area-inset-bottom))] pt-1.5 text-ink shadow-[0_-8px_24px_rgb(24_24_27_/_8%)] backdrop-blur-xl md:hidden" aria-label="Mobile navigation">
    {#each navigationItems as item}
      {@const active = isNavItemActive(item, currentPath)}
      <a class={active ? 'flex min-w-0 flex-col items-center gap-1 rounded-[14px] bg-soft px-2 py-2 text-center text-[0.7rem] font-semibold text-accent no-underline' : 'flex min-w-0 flex-col items-center gap-1 rounded-[14px] px-2 py-2 text-center text-[0.7rem] font-semibold text-muted no-underline hover:bg-cream hover:text-ink'} href={item.href} aria-current={active ? 'page' : undefined}>
        {#if item.icon === 'discover'}
          <Compass class="size-4" aria-hidden="true" />
        {:else if item.icon === 'search'}
          <Search class="size-4" aria-hidden="true" />
        {:else if item.icon === 'saved'}
          <Bookmark class="size-4" aria-hidden="true" />
        {:else if item.icon === 'account'}
          <UserCircle class="size-4" aria-hidden="true" />
        {:else}
          <Shield class="size-4" aria-hidden="true" />
        {/if}
        <span class="truncate">{item.label}</span>
      </a>
    {/each}
  </nav>
</div>
