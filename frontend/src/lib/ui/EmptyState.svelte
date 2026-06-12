<script lang="ts">
  import type { Snippet } from 'svelte';
  import Card from './Card.svelte';

  let { title, message, children }: { title: string; message?: string | null; children?: Snippet } = $props();
</script>

<Card class="grid gap-3">
  <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">{title}</h2>
  {#if message}<p class="m-0 text-muted">{message}</p>{/if}
  {#if children}<div class="mt-2 flex flex-wrap gap-2">{@render children()}</div>{/if}
</Card>
