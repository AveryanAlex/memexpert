<script lang="ts">
  import type { Snippet } from 'svelte';
  import { Badge } from '$lib/ui';

  let { title, count, children }: { title: string; count: string; children?: Snippet } = $props();
</script>

<section class="mt-7" aria-labelledby={title.toLowerCase().replace(/\s+/g, '-') + '-title'}>
  <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
    <h2 id={title.toLowerCase().replace(/\s+/g, '-') + '-title'} class="m-0 text-2xl font-black tracking-[-0.04em]">{title}</h2>
    <Badge>{count}</Badge>
  </div>
  {#if children}{@render children()}{/if}
</section>
