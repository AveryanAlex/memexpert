<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLButtonAttributes } from 'svelte/elements';
  import { cn, focusRing } from './styles';

  interface Props extends HTMLButtonAttributes {
    variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
    size?: 'md' | 'compact' | 'icon';
    children?: Snippet;
  }

  let { variant = 'primary', size = 'md', class: className = '', children, ...rest }: Props = $props();

  const variantClass = $derived(
    variant === 'secondary'
      ? 'border border-line bg-paper text-ink hover:bg-soft'
      : variant === 'danger'
        ? 'bg-danger text-paper hover:bg-danger/90'
        : variant === 'ghost'
          ? 'bg-transparent text-ink hover:bg-soft'
          : 'bg-ink text-paper hover:bg-ink/90'
  );
  const sizeClass = $derived(
    size === 'compact'
      ? 'rounded-[14px] px-3 py-2 text-sm'
      : size === 'icon'
        ? 'grid size-11 place-items-center rounded-full p-0'
        : 'rounded-[18px] px-5 py-4'
  );
</script>

<button
  {...rest}
  class={cn(
    'inline-flex items-center justify-center gap-2 font-extrabold no-underline transition disabled:cursor-not-allowed disabled:opacity-60',
    focusRing,
    variantClass,
    sizeClass,
    className
  )}
>
  {#if children}{@render children()}{/if}
</button>
