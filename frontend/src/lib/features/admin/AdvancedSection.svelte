<script lang="ts">
  import type { Snippet } from 'svelte';
  import { cn } from '$lib/ui/styles';

  let {
    title,
    description,
    danger = false,
    open = $bindable(false),
    class: className = '',
    children
  }: {
    title: string;
    description?: string;
    danger?: boolean;
    open?: boolean;
    class?: string;
    children?: Snippet;
  } = $props();
</script>

<details class={cn('rounded-2xl border p-4', danger ? 'border-danger-line bg-danger-surface' : 'border-line bg-soft/40', className)} bind:open>
  <summary class="cursor-pointer list-none font-extrabold text-ink marker:content-none">
    <span class="flex items-center justify-between gap-3">
      {title}
      <span aria-hidden="true" class="text-muted">⌄</span>
    </span>
  </summary>
  {#if description}<p class="mb-0 mt-2 text-sm text-muted">{description}</p>{/if}
  {#if children}<div class="mt-4">{@render children()}</div>{/if}
</details>
