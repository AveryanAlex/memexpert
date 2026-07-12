<script lang="ts">
  import type { Snippet } from 'svelte';
  import { cn } from './styles';

  let { class: className = '', children }: { class?: string; children?: Snippet } = $props();
</script>

<div class={cn('mx-auto w-[min(1200px,calc(100%_-_32px))] py-6 pb-10', className)}>
  {#if children}{@render children()}{/if}
</div>
