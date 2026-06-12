<script lang="ts">
  import { Tooltip } from 'bits-ui';
  import type { Snippet } from 'svelte';

  let { children, ...rest }: { children?: Snippet; [key: string]: unknown } = $props();
</script>

<Tooltip.Trigger {...rest}>
  {#if children}{@render children()}{/if}
</Tooltip.Trigger>
