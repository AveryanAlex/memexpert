<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLAnchorAttributes } from 'svelte/elements';
  import { cn, focusRing } from './styles';

  interface Props extends HTMLAnchorAttributes {
    active?: boolean;
    size?: 'md' | 'compact';
    children?: Snippet;
  }

  let {
    active = false,
    size = 'md',
    class: className = '',
    children,
    'aria-current': ariaCurrent,
    ...rest
  }: Props = $props();

  const stateClass = $derived(
    active
      ? 'border-ink bg-ink text-paper hover:bg-ink/90'
      : 'border-line bg-paper text-ink hover:bg-soft'
  );
  const sizeClass = $derived(
    size === 'compact' ? 'px-3 py-1.5 text-sm font-semibold' : 'px-4 py-3 font-extrabold'
  );
  const resolvedAriaCurrent = $derived(ariaCurrent ?? (active ? 'page' : undefined));
</script>

<a
  {...rest}
  aria-current={resolvedAriaCurrent}
  class={cn(
    'inline-flex shrink-0 items-center justify-center rounded-full border no-underline transition-colors',
    focusRing,
    stateClass,
    sizeClass,
    className
  )}
>
  {#if children}{@render children()}{/if}
</a>
