<script lang="ts">
  import { UserCircle } from '@lucide/svelte';
  import type { Snippet } from 'svelte';
  import type { CurrentSessionRead } from '$lib/api/types';
  import { Button, PageShell } from '$lib/ui';
  import GlobalSearch from './GlobalSearch.svelte';
  import { ADMIN_NAV_ITEM, isNavItemActive, PRIMARY_NAV_ITEMS, profileLabel, providerSummary } from './navigation';

  let {
    session,
    sessionError,
    currentPath = '/',
    onLoginClick,
    children
  }: { session: CurrentSessionRead | null; sessionError: string | null; currentPath?: string; onLoginClick?: () => void; children?: Snippet } = $props();

  const navigationItems = $derived(session?.user.is_admin ? [...PRIMARY_NAV_ITEMS, ADMIN_NAV_ITEM] : PRIMARY_NAV_ITEMS);
</script>

<div class="min-h-screen bg-[radial-gradient(circle_at_top_left,#1d4ed8_0,#0f172a_34%,#020617_100%)] text-white">
  <header class="sticky top-0 z-30 border-b border-white/10 bg-slate-950/75 backdrop-blur-xl">
    <div class="mx-auto flex w-[min(1280px,calc(100%_-_24px))] items-center gap-4 py-3">
      <a class="shrink-0 text-xl font-black tracking-[-0.06em] text-white no-underline" href="/">MemeXpert</a>
      <nav class="hidden items-center gap-1 lg:flex" aria-label="Primary navigation">
        {#each navigationItems as item}
          {@const active = isNavItemActive(item, currentPath)}
          <a class={active ? 'rounded-full bg-white px-4 py-2 text-sm font-black text-slate-950 no-underline' : 'rounded-full px-4 py-2 text-sm font-black text-slate-300 no-underline hover:bg-white/10 hover:text-white'} href={item.href} aria-current={active ? 'page' : undefined}>{item.label}</a>
        {/each}
      </nav>
      <GlobalSearch />
      {#if session?.user.account_type === 'full'}
        <a class="hidden items-center gap-2 rounded-full border border-white/10 bg-white/10 px-4 py-2 text-sm font-black text-white no-underline hover:bg-white/15 md:flex" href="/profile"><UserCircle class="size-4" aria-hidden="true" /> {profileLabel(session)} <span class="text-xs text-slate-300">{providerSummary(session)}</span></a>
      {:else}
        <div class="hidden items-center gap-2 md:flex">
          <span class="rounded-full border border-white/10 bg-white/10 px-3 py-2 text-xs font-black text-slate-200">{profileLabel(session)}</span>
          <Button class="rounded-full bg-[#229ED9] px-4 py-2 text-white hover:bg-[#1d8fc5]" type="button" onclick={() => onLoginClick?.()}>Sign in</Button>
        </div>
      {/if}
    </div>
    {#if sessionError}<p class="mx-auto m-0 w-[min(1280px,calc(100%_-_24px))] pb-2 text-xs text-amber-200">{sessionError}</p>{/if}
  </header>

  <PageShell class="w-[min(1280px,calc(100%_-_24px))] pb-28 pt-6 text-slate-950 md:pb-12">
    {#if children}{@render children()}{/if}
  </PageShell>

  <nav class={session?.user.is_admin ? 'fixed inset-x-3 bottom-3 z-30 grid grid-cols-4 rounded-[28px] border border-white/10 bg-slate-950/90 p-2 text-white shadow-[0_24px_60px_rgb(2_6_23_/_45%)] backdrop-blur-xl md:hidden' : 'fixed inset-x-3 bottom-3 z-30 grid grid-cols-3 rounded-[28px] border border-white/10 bg-slate-950/90 p-2 text-white shadow-[0_24px_60px_rgb(2_6_23_/_45%)] backdrop-blur-xl md:hidden'} aria-label="Mobile navigation">
    {#each navigationItems as item}
      {@const active = isNavItemActive(item, currentPath)}
      <a class={active ? 'rounded-2xl bg-white px-2 py-3 text-center text-xs font-black text-slate-950 no-underline' : 'rounded-2xl px-2 py-3 text-center text-xs font-black text-slate-300 no-underline'} href={item.href} aria-current={active ? 'page' : undefined}>{item.label}</a>
    {/each}
  </nav>
</div>
