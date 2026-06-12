<script lang="ts">
  import { Popover } from 'bits-ui';
  import type { Snippet } from 'svelte';

  let { open = $bindable(false), children, ...rest }: { open?: boolean; children?: Snippet; [key: string]: unknown } = $props();
</script>

<Popover.Root bind:open {...rest}>
  {#if children}{@render children()}{/if}
</Popover.Root>
