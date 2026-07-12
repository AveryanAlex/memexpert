<script lang="ts">
  import { onMount, type Snippet } from 'svelte';
  import AdminNavigation from './AdminNavigation.svelte';

  let { currentPath, children }: { currentPath: string; children?: Snippet } = $props();
  let hydrated = $state(false);

  onMount(() => {
    hydrated = true;
  });
</script>

<div class="grid gap-6 lg:grid-cols-[15rem_minmax(0,1fr)]" data-admin-shell data-admin-hydrated={hydrated ? 'true' : 'false'}>
  <aside class="hidden self-start lg:sticky lg:top-24 lg:block">
    <div class="rounded-3xl border border-line bg-paper p-3 shadow-[0_16px_40px_rgb(64_46_26_/_8%)]">
      <p class="m-3 text-lg font-black tracking-[-0.04em] text-ink">Admin</p>
      <AdminNavigation {currentPath} />
    </div>
  </aside>

  <div class="min-w-0">
    <div class="-mx-1 mb-6 overflow-x-auto px-1 pb-2 lg:hidden">
      <AdminNavigation {currentPath} variant="mobile" />
    </div>
    {#if children}{@render children()}{/if}
  </div>
</div>
