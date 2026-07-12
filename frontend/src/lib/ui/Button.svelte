<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLButtonAttributes } from 'svelte/elements';
  import { cn, focusRing } from './styles';

  interface Props extends HTMLButtonAttributes {
    variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
    size?: 'md' | 'compact' | 'micro' | 'icon';
    children?: Snippet;
  }

  let { variant = 'primary', size = 'md', class: className = '', children, ...rest }: Props = $props();

  const variantClass = $derived(
    variant === 'secondary'
      ? 'border border-line bg-paper text-ink hover:bg-soft'
      : variant === 'danger'
        ? 'bg-danger text-on-danger hover:bg-danger/90'
        : variant === 'ghost'
          ? 'bg-transparent text-ink hover:bg-soft'
          : 'bg-accent text-on-accent hover:bg-accent/90'
  );
  const sizeClass = $derived(
    size === 'compact'
      ? 'rounded-[14px] px-3 py-2 text-sm'
      : size === 'micro'
        ? 'gap-1 rounded-[10px] px-1 py-2 text-[0.7rem] leading-none'
      : size === 'icon'
        ? 'grid size-10 place-items-center rounded-[14px] p-0'
        : 'rounded-[16px] px-4 py-2.5'
  );
</script>

<button
  {...rest}
  class={cn(
    'inline-flex items-center justify-center gap-2 font-semibold no-underline transition disabled:cursor-not-allowed disabled:opacity-60',
    focusRing,
    variantClass,
    sizeClass,
    className
  )}
>
  {#if children}{@render children()}{/if}
</button>
