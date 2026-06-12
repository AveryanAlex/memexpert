<script lang="ts">
  import { Dialog } from 'bits-ui';
  import type { Snippet } from 'svelte';

  let { open = $bindable(false), children, ...rest }: { open?: boolean; children?: Snippet; [key: string]: unknown } = $props();
</script>

<Dialog.Root bind:open {...rest}>
  {#if children}{@render children()}{/if}
</Dialog.Root>
