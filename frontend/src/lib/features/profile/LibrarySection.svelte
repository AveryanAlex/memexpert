<script lang="ts">
  import type { Snippet } from 'svelte';
  import { Badge } from '$lib/ui';

  let { id, title, count, children }: { id?: string; title: string; count: string; children?: Snippet } = $props();

  const titleId = $derived(`${id ?? title.toLowerCase().replace(/\s+/g, '-')}-title`);
</script>

<section {id} class="mt-7" aria-labelledby={titleId}>
  <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
    <h2 id={titleId} class="m-0 text-2xl font-black tracking-[-0.04em]">{title}</h2>
    <Badge>{count}</Badge>
  </div>
  {#if children}{@render children()}{/if}
</section>
