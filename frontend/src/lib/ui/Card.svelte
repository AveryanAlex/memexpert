<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLAttributes } from 'svelte/elements';
  import { cn, panelClass } from './styles';

  interface Props extends HTMLAttributes<HTMLElement> {
    elevated?: boolean;
    children?: Snippet;
  }

  let { elevated = false, class: className = '', children, ...rest }: Props = $props();
</script>

<section {...rest} class={cn(panelClass, elevated ? 'shadow-overlay' : '', className)}>
  {#if children}{@render children()}{/if}
</section>
