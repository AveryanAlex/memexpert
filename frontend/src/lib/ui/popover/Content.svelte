<script lang="ts">
  import { Popover } from 'bits-ui';
  import type { Snippet } from 'svelte';
  import { cn } from '../styles';

  let {
    variant = 'panel',
    class: className = '',
    align = 'center',
    sideOffset = 8,
    children,
    ...rest
  }: {
    variant?: 'panel' | 'floating';
    class?: string;
    align?: 'start' | 'center' | 'end';
    sideOffset?: number;
    children?: Snippet;
    [key: string]: unknown;
  } = $props();
</script>

<Popover.Portal>
  <Popover.Content
    {...rest}
    {align}
    {sideOffset}
    class={cn(
      'z-30 grid max-w-80 gap-3 rounded-[22px] bg-paper p-4 text-ink shadow-warm-lg',
      variant === 'panel' && 'border border-line',
      className
    )}
  >
    {#if children}{@render children()}{/if}
  </Popover.Content>
</Popover.Portal>
