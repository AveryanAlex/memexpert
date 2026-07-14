<script lang="ts">
  import { Popover } from 'bits-ui';
  import type { Snippet } from 'svelte';
  import { cn, focusRing } from '../styles';

  let {
    variant = 'default',
    class: className = '',
    children,
    ...rest
  }: {
    variant?: 'default' | 'ghost' | 'secondary';
    class?: string;
    children?: Snippet;
    [key: string]: unknown;
  } = $props();
</script>

<Popover.Trigger
  {...rest}
  class={cn(
    variant === 'ghost'
      ? 'inline-flex h-10 w-full items-center justify-center rounded-[10px] bg-transparent text-ink transition hover:bg-soft disabled:cursor-progress disabled:opacity-65'
      : variant === 'secondary'
        ? 'inline-flex items-center justify-center gap-2 rounded-[16px] border border-line bg-paper px-4 py-2.5 font-semibold text-ink transition hover:bg-soft disabled:cursor-progress disabled:opacity-65'
        : 'inline-flex items-center justify-center rounded-[18px] border border-line bg-paper px-4 py-3 font-extrabold text-ink',
    focusRing,
    className
  )}
>
  {#if children}{@render children()}{/if}
</Popover.Trigger>
