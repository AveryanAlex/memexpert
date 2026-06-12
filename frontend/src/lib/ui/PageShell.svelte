<script lang="ts">
  import type { Snippet } from 'svelte';
  import { cn } from './styles';

  let { class: className = '', children }: { class?: string; children?: Snippet } = $props();
</script>

<div class={cn('mx-auto w-[min(1120px,calc(100%_-_32px))] py-8 pb-12', className)}>
  {#if children}{@render children()}{/if}
</div>
