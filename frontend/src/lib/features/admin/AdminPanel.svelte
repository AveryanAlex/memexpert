<script lang="ts">
  import type { Snippet } from 'svelte';
  import { Card } from '$lib/ui';
  import { cn } from '$lib/ui/styles';

  let { title, class: className = '', children }: { title?: string; class?: string; children?: Snippet } = $props();
</script>

<Card class={cn('mt-4 grid gap-3', className)}>
  {#if title}<h2 class="m-0 text-2xl font-black tracking-[-0.04em]">{title}</h2>{/if}
  {#if children}{@render children()}{/if}
</Card>
