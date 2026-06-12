<script lang="ts">
  import { DropdownMenu } from 'bits-ui';
  import type { Snippet } from 'svelte';

  let { open = $bindable(false), children, ...rest }: { open?: boolean; children?: Snippet; [key: string]: unknown } = $props();
</script>

<DropdownMenu.Root bind:open {...rest}>
  {#if children}{@render children()}{/if}
</DropdownMenu.Root>
