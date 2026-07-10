<script lang="ts">
  import { page } from '$app/state';
  import type { Snippet } from 'svelte';
  import AdminShell from '$lib/features/admin/AdminShell.svelte';

  let { children }: { children: Snippet } = $props();
</script>

<AdminShell currentPath={page.url.pathname}>
  {@render children()}
</AdminShell>
