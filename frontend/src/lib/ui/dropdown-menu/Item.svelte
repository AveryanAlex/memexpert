<script lang="ts">
  import { DropdownMenu } from 'bits-ui';
  import type { Snippet } from 'svelte';
  import { cn } from '../styles';

  let { tone = 'neutral', class: className = '', children, ...rest }: { tone?: 'neutral' | 'danger'; class?: string; children?: Snippet; [key: string]: unknown } = $props();
</script>

<DropdownMenu.Item
  {...rest}
  class={cn(
    'flex cursor-pointer select-none items-center gap-2 rounded-xl px-3 py-2.5 font-bold outline-none data-[disabled]:cursor-not-allowed data-[highlighted]:bg-soft data-[disabled]:text-muted/70',
    tone === 'danger' && 'text-danger',
    className
  )}
>
  {#if children}{@render children()}{/if}
</DropdownMenu.Item>
