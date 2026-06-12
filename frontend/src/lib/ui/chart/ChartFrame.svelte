<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLAttributes } from 'svelte/elements';
  import LoadingState from '../LoadingState.svelte';
  import { cn } from '../styles';

  interface Props extends HTMLAttributes<HTMLElement> {
    label: string;
    description?: string | null;
    loading?: boolean;
    loadingLabel?: string;
    empty?: boolean;
    emptyTitle?: string;
    emptyMessage?: string | null;
    size?: 'compact' | 'default' | 'tall';
    showCaption?: boolean;
    captionClass?: string;
    plotClass?: string;
    footer?: Snippet;
    children?: Snippet;
  }

  let {
    label,
    description = null,
    loading = false,
    loadingLabel = 'Loading chart',
    empty = false,
    emptyTitle = 'No chart data yet',
    emptyMessage = null,
    size = 'default',
    showCaption = true,
    captionClass = '',
    plotClass = '',
    footer,
    children,
    class: className = '',
    ...rest
  }: Props = $props();

  const heightClass = $derived(
    size === 'compact'
      ? 'min-h-40 sm:min-h-48'
      : size === 'tall'
        ? 'min-h-80 sm:min-h-96'
        : 'min-h-64 sm:min-h-72'
  );
</script>

<figure {...rest} aria-busy={loading ? 'true' : undefined} class={cn('m-0 grid gap-3 text-ink', className)}>
  <figcaption class={cn(showCaption ? 'grid gap-1' : 'sr-only', captionClass)}>
    <span class="text-sm font-extrabold uppercase tracking-[0.18em] text-ink">{label}</span>
    {#if description}<span class="text-sm text-muted">{description}</span>{/if}
  </figcaption>

  <div
    class={cn(
      'relative w-full overflow-hidden rounded-[24px] border border-line bg-gradient-to-b from-paper to-soft p-3 text-ink',
      heightClass,
      plotClass
    )}
  >
    {#if loading}
      <div class="grid h-full min-h-[inherit] place-items-center">
        <LoadingState label={loadingLabel} />
      </div>
    {:else if empty}
      <div class="grid h-full min-h-[inherit] place-items-center rounded-2xl border border-dashed border-line bg-paper/80 p-5 text-center">
        <div class="grid max-w-sm gap-2">
          <p class="m-0 font-extrabold text-ink">{emptyTitle}</p>
          {#if emptyMessage}<p class="m-0 text-sm text-muted">{emptyMessage}</p>{/if}
        </div>
      </div>
    {:else if children}
      {@render children()}
    {/if}
  </div>

  {#if footer}
    <div class="flex flex-wrap gap-2 text-sm text-muted">
      {@render footer()}
    </div>
  {/if}
</figure>
