<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLAttributes } from 'svelte/elements';
  import { cn, panelClass } from './styles';

  interface Props extends HTMLAttributes<HTMLElement> {
    children?: Snippet;
  }

  let { class: className = '', children, ...rest }: Props = $props();
</script>

<section {...rest} class={cn(panelClass, className)}>
  {#if children}{@render children()}{/if}
</section>
