<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLLabelAttributes } from 'svelte/elements';
  import { cn } from './styles';

  interface Props extends HTMLLabelAttributes {
    children?: Snippet;
  }

  let { class: className = '', children, ...rest }: Props = $props();
</script>

<label {...rest} class={cn('grid gap-2 font-extrabold text-chiptext', className)}>
  {#if children}{@render children()}{/if}
</label>
