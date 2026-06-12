<script lang="ts">
  import type { Snippet } from 'svelte';
  import { cn } from './styles';

  let { label, hint = null, class: className = '', children }: { label?: string; hint?: string | null; class?: string; children?: Snippet } = $props();
</script>

<label class={cn('grid gap-2 text-chiptext', className)}>
  {#if label}<span class="font-extrabold">{label}</span>{/if}
  {#if children}{@render children()}{/if}
  {#if hint}<span class="text-sm font-normal text-muted">{hint}</span>{/if}
</label>
