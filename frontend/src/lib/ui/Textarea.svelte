<script lang="ts">
  import type { HTMLTextareaAttributes } from 'svelte/elements';
  import { cn, fieldClass, focusRing } from './styles';

  interface Props extends Omit<HTMLTextareaAttributes, 'class' | 'value'> {
    class?: string;
    value?: HTMLTextareaAttributes['value'];
  }

  let { class: className = '', value = $bindable(), ...rest }: Props = $props();
</script>

<textarea {...rest} bind:value class={cn(fieldClass, focusRing, 'resize-y', className)}></textarea>
