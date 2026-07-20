<script lang="ts" generics="Item">
  import { onMount, tick, type Snippet } from 'svelte';
  import type { Attachment } from 'svelte/attachments';
  import type { HTMLAttributes } from 'svelte/elements';
  import { cn } from '../styles';
  import {
    calculateMasonryLayout,
    DEFAULT_MASONRY_GAP,
    DEFAULT_MASONRY_MAX_COLUMNS,
    DEFAULT_MASONRY_MIN_COLUMN_WIDTH,
    masonryColumnCount
  } from './layout';
  import type { MasonryElement, MasonryItemLayout, MasonryKey, MasonryPosition } from './types';

  interface Props extends Omit<HTMLAttributes<HTMLElement>, 'children'> {
    items: Item[];
    getKey: (item: Item, index: number) => MasonryKey;
    minColumnWidth?: number;
    gap?: number;
    maxColumns?: number;
    element?: MasonryElement;
    busy?: boolean;
    columnCount?: number;
    ready?: boolean;
    children: Snippet<[Item, number, MasonryItemLayout]>;
  }

  let {
    items,
    getKey,
    minColumnWidth = DEFAULT_MASONRY_MIN_COLUMN_WIDTH,
    gap = DEFAULT_MASONRY_GAP,
    maxColumns = DEFAULT_MASONRY_MAX_COLUMNS,
    element = 'div',
    busy = false,
    columnCount = $bindable(1),
    ready = $bindable(false),
    class: className = '',
    children: renderItem,
    ...rest
  }: Props = $props();

  let rootElement = $state<HTMLElement>();
  let observer: ResizeObserver | null = null;
  let layoutFrame = 0;
  let layoutGeneration = 0;
  let arranging = $state(false);
  let positioned = $state(false);
  let fallback = $state(false);
  let columnWidth = $state(0);
  let containerHeight = $state(0);
  let positions = $state(new Map<MasonryKey, MasonryPosition>());

  // Once the first complete placement is visible, keep the root ready. Later appends withhold only
  // their unplaced wrappers, and measured relayouts never flash the whole surface back to pending.
  const masonryState = $derived(fallback || positioned ? 'ready' : 'pending');
  const ariaBusy = $derived(
    busy || arranging || rest['aria-busy'] === true || rest['aria-busy'] === 'true' ? 'true' : undefined
  );
  const itemLayout = $derived<MasonryItemLayout>({ columnCount, ready });
  const normalizedGap = $derived(nonNegativeFinite(gap));
  const normalizedMinimum = $derived(positiveFinite(minColumnWidth, DEFAULT_MASONRY_MIN_COLUMN_WIDTH));
  const normalizedMaximum = $derived(positiveInteger(maxColumns, DEFAULT_MASONRY_MAX_COLUMNS));
  const fallbackColumnWidth = $derived(
    `max(${normalizedMinimum}px, calc(${100 / normalizedMaximum}% - ${(normalizedGap * (normalizedMaximum - 1)) / normalizedMaximum}px))`
  );

  const observeItem: Attachment<HTMLElement> = (node) => {
    observer?.observe(node);
    scheduleLayout();

    return () => {
      observer?.unobserve(node);
      scheduleLayout();
    };
  };

  $effect(() => {
    items;
    getKey;
    minColumnWidth;
    gap;
    maxColumns;
    scheduleLayout();
  });

  onMount(() => {
    if (!rootElement) return;

    if (typeof ResizeObserver !== 'function' || typeof window.requestAnimationFrame !== 'function') {
      activateFallback();
      return;
    }

    observer = new ResizeObserver(() => scheduleLayout());
    observer.observe(rootElement);
    for (const node of itemNodes()) observer.observe(node);
    scheduleLayout();

    return () => {
      observer?.disconnect();
      observer = null;
      window.cancelAnimationFrame(layoutFrame);
      layoutFrame = 0;
      layoutGeneration += 1;
    };
  });

  function scheduleLayout() {
    if (!rootElement || fallback || typeof window === 'undefined' || typeof window.requestAnimationFrame !== 'function') {
      return;
    }

    arranging = true;
    layoutGeneration += 1;
    const generation = layoutGeneration;
    window.cancelAnimationFrame(layoutFrame);
    layoutFrame = window.requestAnimationFrame(() => {
      layoutFrame = 0;
      void performLayout(generation);
    });
  }

  async function performLayout(generation: number) {
    if (!rootElement || generation !== layoutGeneration) return;

    const containerWidth = rootElement.clientWidth;
    if (containerWidth <= 0) return;

    const nextColumnCount = masonryColumnCount(containerWidth, minColumnWidth, gap, maxColumns);
    const nextColumnWidth = Math.max(0, (containerWidth - normalizedGap * (nextColumnCount - 1)) / nextColumnCount);
    if (columnCount !== nextColumnCount) columnCount = nextColumnCount;
    if (columnWidth !== nextColumnWidth) columnWidth = nextColumnWidth;

    // Apply the final responsive width while the initial list is still hidden. The same tick makes
    // responsive relayout measurements authoritative instead of reusing heights from an old width.
    await tick();

    if (!rootElement || generation !== layoutGeneration) return;

    const nodes = itemNodes();
    if (nodes.length !== items.length) {
      scheduleLayout();
      return;
    }

    const layout = calculateMasonryLayout({
      containerWidth,
      itemHeights: nodes.map((node) => node.getBoundingClientRect().height),
      minColumnWidth,
      gap,
      maxColumns
    });
    const nextPositions = new Map<MasonryKey, MasonryPosition>();

    for (const [index, item] of items.entries()) {
      const position = layout.positions[index];
      if (position) nextPositions.set(getKey(item, index), position);
    }

    if (generation !== layoutGeneration) return;

    columnCount = layout.columnCount;
    columnWidth = layout.columnWidth;
    containerHeight = layout.height;
    positions = nextPositions;
    positioned = true;
    ready = true;
    arranging = false;
  }

  function activateFallback() {
    if (!rootElement) return;

    columnCount = masonryColumnCount(rootElement.clientWidth, minColumnWidth, gap, maxColumns);
    ready = true;
    fallback = true;
    arranging = false;
  }

  function itemNodes(): HTMLElement[] {
    if (!rootElement) return [];
    return Array.from(rootElement.querySelectorAll<HTMLElement>(':scope > [data-masonry-item]'));
  }

  function itemPosition(item: Item, index: number): MasonryPosition | undefined {
    return positions.get(getKey(item, index));
  }

  function nonNegativeFinite(value: number): number {
    return Number.isFinite(value) ? Math.max(0, value) : 0;
  }

  function positiveFinite(value: number, fallbackValue: number): number {
    return Number.isFinite(value) && value > 0 ? value : fallbackValue;
  }

  function positiveInteger(value: number, fallbackValue: number): number {
    return Number.isFinite(value) && value >= 1 ? Math.floor(value) : fallbackValue;
  }
</script>

<svelte:element
  this={element}
  bind:this={rootElement}
  {...rest}
  class={cn('masonry-root', className)}
  aria-busy={ariaBusy}
  data-layout="masonry"
  data-column-count={columnCount}
  data-masonry-state={masonryState}
  data-masonry-positioned={positioned ? 'true' : undefined}
  data-masonry-fallback={fallback ? 'true' : undefined}
  style:--masonry-gap={`${normalizedGap}px`}
  style:--masonry-column-count={columnCount}
  style:--masonry-column-width={`${columnWidth}px`}
  style:--masonry-height={`${containerHeight}px`}
  style:--masonry-fallback-column-width={fallbackColumnWidth}
>
  {#each items as item, index (getKey(item, index))}
    {@const position = itemPosition(item, index)}
    <div
      {@attach observeItem}
      class="masonry-item"
      role="presentation"
      data-masonry-item
      data-masonry-index={index}
      data-masonry-column={position?.column}
      data-masonry-item-state={fallback || (positioned && position) ? 'ready' : 'pending'}
      style:--masonry-x={`${position?.x ?? 0}px`}
      style:--masonry-y={`${position?.y ?? 0}px`}
    >
      {@render renderItem(item, index, itemLayout)}
    </div>
  {/each}
</svelte:element>

<style>
  .masonry-root {
    display: grid;
    grid-template-columns: repeat(var(--masonry-column-count), minmax(0, 1fr));
    align-items: start;
    gap: var(--masonry-gap);
  }

  .masonry-root[data-masonry-positioned='true'] {
    position: relative;
    display: block;
    height: var(--masonry-height);
  }

  .masonry-item {
    display: flow-root;
    min-width: 0;
    max-width: 100%;
    visibility: hidden;
  }

  .masonry-item[data-masonry-item-state='ready'] {
    visibility: visible;
  }

  .masonry-root[data-masonry-positioned='true'] > .masonry-item {
    position: absolute;
    top: 0;
    left: 0;
    width: var(--masonry-column-width);
    transform: translate3d(var(--masonry-x), var(--masonry-y), 0);
  }

  .masonry-root[data-masonry-fallback='true'] {
    position: static;
    display: grid;
    height: auto;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, var(--masonry-fallback-column-width)), 1fr));
  }

  .masonry-root[data-masonry-fallback='true'] > .masonry-item {
    position: static;
    width: auto;
    visibility: visible;
    transform: none;
  }

  @media (scripting: none) {
    .masonry-root {
      position: static !important;
      display: grid !important;
      height: auto !important;
      grid-template-columns: repeat(auto-fill, minmax(min(100%, var(--masonry-fallback-column-width)), 1fr)) !important;
    }

    .masonry-root > .masonry-item {
      position: static !important;
      width: auto !important;
      visibility: visible !important;
      transform: none !important;
    }
  }
</style>
