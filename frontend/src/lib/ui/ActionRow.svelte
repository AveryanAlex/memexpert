<script lang="ts">
  import type { Snippet } from 'svelte';
  import { cn } from './styles';

  let { class: className = '', children }: { class?: string; children?: Snippet } = $props();
</script>

<div class={cn('mt-6 flex flex-wrap items-center gap-2', className)}>
  {#if children}{@render children()}{/if}
</div>
