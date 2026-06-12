<script lang="ts">
  import { Tooltip } from 'bits-ui';
  import type { Snippet } from 'svelte';

  let { open = $bindable(false), children, ...rest }: { open?: boolean; children?: Snippet; [key: string]: unknown } = $props();
</script>

<Tooltip.Root bind:open {...rest}>
  {#if children}{@render children()}{/if}
</Tooltip.Root>
