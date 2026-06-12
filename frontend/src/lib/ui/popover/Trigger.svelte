<script lang="ts">
  import { Popover } from 'bits-ui';
  import type { Snippet } from 'svelte';
  import { cn, focusRing } from '../styles';

  let { class: className = '', children, ...rest }: { class?: string; children?: Snippet; [key: string]: unknown } = $props();
</script>

<Popover.Trigger {...rest} class={cn('inline-flex items-center justify-center rounded-[18px] border border-line bg-paper px-4 py-3 font-extrabold text-ink', focusRing, className)}>
  {#if children}{@render children()}{/if}
</Popover.Trigger>
