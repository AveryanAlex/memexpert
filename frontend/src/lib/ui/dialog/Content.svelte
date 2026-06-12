<script lang="ts">
  import { Dialog } from 'bits-ui';
  import type { Snippet } from 'svelte';
  import { cn } from '../styles';

  let { class: className = '', children, ...rest }: { class?: string; children?: Snippet; [key: string]: unknown } = $props();
</script>

<Dialog.Portal>
  <Dialog.Overlay class="fixed inset-0 z-40 bg-ink/45" />
  <Dialog.Content {...rest} class={cn('fixed left-1/2 top-1/2 z-50 grid w-[min(92vw,36rem)] -translate-x-1/2 -translate-y-1/2 gap-4 rounded-[28px] border border-line bg-paper p-6 text-ink shadow-warm-lg', className)}>
    {#if children}{@render children()}{/if}
  </Dialog.Content>
</Dialog.Portal>
