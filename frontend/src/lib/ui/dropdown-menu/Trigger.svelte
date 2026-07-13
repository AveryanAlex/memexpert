<script lang="ts">
  import { DropdownMenu } from 'bits-ui';
  import type { Snippet } from 'svelte';
  import { cn, focusRing } from '../styles';

  let {
    variant = 'floating',
    class: className = '',
    children,
    ...rest
  }: {
    variant?: 'floating' | 'ghost' | 'secondary';
    class?: string;
    children?: Snippet;
    [key: string]: unknown;
  } = $props();
</script>

<DropdownMenu.Trigger
  {...rest}
  class={cn(
    variant === 'ghost'
      ? 'inline-flex h-10 w-full items-center justify-center rounded-[10px] bg-transparent text-ink transition hover:bg-soft disabled:cursor-progress disabled:opacity-65'
      : variant === 'secondary'
        ? 'inline-flex items-center justify-center gap-2 rounded-[16px] border border-line bg-paper px-4 py-2.5 font-semibold text-ink transition hover:bg-soft disabled:cursor-progress disabled:opacity-65'
        : 'grid size-11 place-items-center rounded-full border border-paper/70 bg-ink/90 text-paper shadow-[0_12px_24px_rgb(23_33_47_/_20%)] transition hover:bg-ink disabled:cursor-progress disabled:opacity-65',
    focusRing,
    className
  )}
>
  {#if children}{@render children()}{/if}
</DropdownMenu.Trigger>
