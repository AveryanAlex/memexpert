<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLAnchorAttributes } from 'svelte/elements';
  import { cn, focusRing } from './styles';

  interface Props extends HTMLAnchorAttributes {
    variant?: 'primary' | 'secondary' | 'ghost';
    size?: 'md' | 'compact';
    children?: Snippet;
  }

  let { variant = 'primary', size = 'md', class: className = '', children, ...rest }: Props = $props();

  const variantClass = $derived(
    variant === 'secondary'
      ? 'border border-line bg-paper text-ink hover:bg-soft'
      : variant === 'ghost'
        ? 'bg-transparent text-muted underline decoration-2 underline-offset-4 hover:text-ink'
        : 'bg-ink text-paper hover:bg-ink/90'
  );
  const sizeClass = $derived(size === 'compact' ? 'rounded-[14px] px-3 py-2 text-sm' : 'rounded-[18px] px-5 py-4');
</script>

<a
  {...rest}
  class={cn('inline-flex items-center justify-center gap-2 font-extrabold no-underline transition', focusRing, variantClass, sizeClass, className)}
>
  {#if children}{@render children()}{/if}
</a>
