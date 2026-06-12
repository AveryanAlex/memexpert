<script lang="ts">
  import type { HTMLInputAttributes } from 'svelte/elements';
  import { cn, fieldClass, focusRing } from './styles';

  interface Props extends Omit<HTMLInputAttributes, 'class' | 'value'> {
    class?: string;
    value?: HTMLInputAttributes['value'];
  }

  let { class: className = '', value = $bindable(), ...rest }: Props = $props();
</script>

<input {...rest} bind:value class={cn(fieldClass, focusRing, className)} />
