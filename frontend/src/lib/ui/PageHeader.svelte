<script lang="ts">
  import type { Snippet } from 'svelte';
  import Badge from './Badge.svelte';
  import { cn } from './styles';

  let {
    title,
    eyebrow = null,
    description = null,
    badge = null,
    class: className = '',
    children
  }: { title: string; eyebrow?: string | null; description?: string | null; badge?: string | null; class?: string; children?: Snippet } = $props();
</script>

<section class={cn('mb-7 grid items-end gap-7 md:grid-cols-[minmax(0,1fr)_auto]', className)}>
  <div>
    {#if eyebrow}<Badge class="mb-4">{eyebrow}</Badge>{/if}
    <h1 class="m-0 mb-3 text-[clamp(2.3rem,8vw,5.6rem)] font-black leading-[0.9] tracking-[-0.075em]">{title}</h1>
    {#if description}<p class="m-0 max-w-3xl text-muted">{description}</p>{/if}
  </div>
  {#if badge}<Badge>{badge}</Badge>{/if}
  {#if children}<div class="flex flex-wrap justify-end gap-2">{@render children()}</div>{/if}
</section>
