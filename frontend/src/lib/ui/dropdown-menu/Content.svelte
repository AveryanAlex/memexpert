<script lang="ts">
  import { DropdownMenu } from 'bits-ui';
  import type { Snippet } from 'svelte';
  import { cn } from '../styles';

  let { class: className = '', align = 'end', sideOffset = 8, children, ...rest }: { class?: string; align?: 'start' | 'center' | 'end'; sideOffset?: number; children?: Snippet; [key: string]: unknown } = $props();
</script>

<DropdownMenu.Portal>
  <DropdownMenu.Content
    {...rest}
    {align}
    {sideOffset}
    class={cn('z-20 min-w-56 rounded-[18px] border border-line bg-paper p-2 text-ink shadow-warm-lg', className)}
  >
    {#if children}{@render children()}{/if}
  </DropdownMenu.Content>
</DropdownMenu.Portal>
