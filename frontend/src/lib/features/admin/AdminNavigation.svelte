<script lang="ts">
  import {
    ADMIN_CATALOG_LINK,
    ADMIN_NAVIGATION_GROUPS,
    isAdminNavigationItemActive,
    type AdminNavigationItem
  } from './navigation';

  let { currentPath, variant = 'sidebar' }: { currentPath: string; variant?: 'mobile' | 'sidebar' } = $props();

  const isMobile = $derived(variant === 'mobile');

  function linkClass(item: AdminNavigationItem): string {
    const active = isAdminNavigationItemActive(item, currentPath);
    if (isMobile) {
      return active
        ? 'shrink-0 rounded-full bg-ink px-4 py-2 text-sm font-extrabold text-paper no-underline'
        : 'shrink-0 rounded-full border border-line bg-paper px-4 py-2 text-sm font-extrabold text-ink no-underline hover:bg-soft';
    }
    return active
      ? 'block rounded-2xl bg-ink px-4 py-3 text-sm font-extrabold text-paper no-underline'
      : 'block rounded-2xl px-4 py-3 text-sm font-extrabold text-ink no-underline hover:bg-soft';
  }
</script>

<nav class={isMobile ? 'flex min-w-max items-center gap-2' : 'grid gap-5'} aria-label="Admin navigation">
  {#each ADMIN_NAVIGATION_GROUPS as group}
    <div class={isMobile ? 'contents' : 'grid gap-1'}>
      {#if !isMobile}<p class="m-0 px-4 text-xs font-black uppercase tracking-[0.16em] text-muted">{group.label}</p>{/if}
      {#each group.items as item (item.href)}
        {@const active = isAdminNavigationItemActive(item, currentPath)}
        <a href={item.href} class={linkClass(item)} aria-current={active ? 'page' : undefined}>{item.label}</a>
      {/each}
    </div>
  {/each}
  <div class={isMobile ? 'contents' : 'border-t border-line pt-4'}>
    <a href={ADMIN_CATALOG_LINK.href} class={isMobile ? 'shrink-0 rounded-full border border-line bg-paper px-4 py-2 text-sm font-extrabold text-ink no-underline hover:bg-soft' : 'block rounded-2xl px-4 py-3 text-sm font-extrabold text-ink no-underline hover:bg-soft'}>{ADMIN_CATALOG_LINK.label}</a>
  </div>
</nav>
