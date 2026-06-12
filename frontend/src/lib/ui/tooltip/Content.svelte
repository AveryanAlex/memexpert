<script lang="ts">
  import { Tooltip } from 'bits-ui';
  import type { Snippet } from 'svelte';
  import { cn } from '../styles';

  let { class: className = '', sideOffset = 6, children, ...rest }: { class?: string; sideOffset?: number; children?: Snippet; [key: string]: unknown } = $props();
</script>

<Tooltip.Portal>
  <Tooltip.Content {...rest} {sideOffset} class={cn('z-40 hidden rounded-xl bg-ink px-3 py-2 text-sm font-bold text-paper shadow-warm-lg md:block', className)}>
    {#if children}{@render children()}{/if}
  </Tooltip.Content>
</Tooltip.Portal>
