<script lang="ts">
  import { Dialog } from 'bits-ui';
  import type { Snippet } from 'svelte';
  import { cn, focusRing } from '../styles';

  let { class: className = '', children, ...rest }: { class?: string; children?: Snippet; [key: string]: unknown } = $props();
</script>

<Dialog.Trigger {...rest} class={cn('inline-flex items-center justify-center rounded-[18px] bg-ink px-5 py-4 font-extrabold text-paper', focusRing, className)}>
  {#if children}{@render children()}{/if}
</Dialog.Trigger>
