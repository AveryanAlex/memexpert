<script lang="ts">
  import type { Snippet } from 'svelte';
  import { cn } from './styles';

  let { tone = 'neutral', class: className = '', children }: { tone?: 'neutral' | 'success' | 'trend'; class?: string; children?: Snippet } = $props();

  const toneClass = $derived(
    tone === 'success'
      ? 'border-success-line bg-success-surface text-success-text'
      : tone === 'trend'
        ? 'border-transparent bg-trend-surface text-trend-text'
        : 'border-line bg-soft text-chiptext'
  );
</script>

<span class={cn('inline-flex items-center rounded-full border px-3 py-1.5 text-sm font-extrabold', toneClass, className)}>
  {#if children}{@render children()}{/if}
</span>
