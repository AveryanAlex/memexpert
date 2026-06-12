<script lang="ts">
  import type { Snippet } from 'svelte';
  import { cn, panelClass } from './styles';

  let { tone = 'neutral', role = 'status', class: className = '', children }: { tone?: 'neutral' | 'danger' | 'success'; role?: string; class?: string; children?: Snippet } = $props();

  const toneClass = $derived(tone === 'danger' ? 'border-danger-line bg-danger-surface text-danger' : tone === 'success' ? 'border-success-line bg-success-surface text-success-text' : '');
</script>

<p {role} class={cn(panelClass, 'my-4', toneClass, className)}>
  {#if children}{@render children()}{/if}
</p>
