<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLSelectAttributes } from 'svelte/elements';
  import { cn, fieldClass, focusRing } from './styles';

  interface Props extends Omit<HTMLSelectAttributes, 'class' | 'value'> {
    class?: string;
    value?: HTMLSelectAttributes['value'];
    children?: Snippet;
  }

  let { class: className = '', value = $bindable(), children, ...rest }: Props = $props();
</script>

<select {...rest} bind:value class={cn(fieldClass, focusRing, className)}>
  {#if children}{@render children()}{/if}
</select>
