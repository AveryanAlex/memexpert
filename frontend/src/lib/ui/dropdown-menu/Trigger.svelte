<script lang="ts">
  import { DropdownMenu } from 'bits-ui';
  import type { Snippet } from 'svelte';
  import { cn, focusRing } from '../styles';

  let { class: className = '', children, ...rest }: { class?: string; children?: Snippet; [key: string]: unknown } = $props();
</script>

<DropdownMenu.Trigger
  {...rest}
  class={cn('grid size-11 place-items-center rounded-full border border-paper/70 bg-ink/90 text-paper shadow-[0_12px_24px_rgb(23_33_47_/_20%)] transition hover:bg-ink disabled:cursor-progress disabled:opacity-65', focusRing, className)}
>
  {#if children}{@render children()}{/if}
</DropdownMenu.Trigger>
