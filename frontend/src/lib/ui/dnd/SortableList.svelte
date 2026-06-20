<script lang="ts" generics="Item extends SortableItem">
  import { DragDropProvider, type DragDropEventHandlers } from '@dnd-kit/svelte';
  import { isSortable } from '@dnd-kit/svelte/sortable';
  import type { Snippet } from 'svelte';
  import type { HTMLAttributes } from 'svelte/elements';
  import { cn } from '../styles';
  import SortableListItem from './SortableListItem.svelte';
  import { moveSortableIdByIndex, orderSortableItemsByIds, sameSortableIds, sortableId, sortableIds } from './sortable';
  import type { SortableId, SortableItem, SortableItemControls } from './types';

  type DragOverEvent = Parameters<NonNullable<DragDropEventHandlers['onDragOver']>>[0];
  type DragEndEvent = Parameters<NonNullable<DragDropEventHandlers['onDragEnd']>>[0];
  type SortableElement = 'article' | 'div' | 'li' | 'section';

  interface Props extends Omit<HTMLAttributes<HTMLElement>, 'children'> {
    items: Item[];
    onReorder: (nextIds: SortableId[]) => void | Promise<void>;
    disabled?: boolean;
    group?: SortableId;
    itemClass?: string;
    itemElement?: SortableElement;
    children: Snippet<[Item, number, SortableItemControls]>;
  }

  let {
    items,
    onReorder,
    disabled = false,
    group,
    itemClass = '',
    itemElement = 'div',
    class: className = '',
    children: renderItem,
    ...rest
  }: Props = $props();

  let previewItems = $state<Item[] | null>(null);
  let dragSnapshot = $state<Item[]>([]);
  let dragging = $state(false);

  const propIds = $derived(sortableIds(items));
  const activeItems = $derived(previewItems ?? items);
  const activeIds = $derived(sortableIds(activeItems));

  $effect(() => {
    if (!dragging && previewItems && sameSortableIds(propIds, activeIds)) {
      previewItems = null;
    }
  });

  function onDragStart() {
    if (disabled) return;
    dragSnapshot = [...activeItems];
    previewItems = [...activeItems];
    dragging = true;
  }

  function onDragOver(event: DragOverEvent) {
    if (disabled) return;

    const { source, target } = event.operation;

    if (!isSortable(source) || !isSortable(target)) return;

    const nextIds = moveSortableIdByIndex(activeIds, source.index, target.index);

    if (!sameSortableIds(nextIds, activeIds)) {
      previewItems = orderSortableItemsByIds(activeItems, nextIds);
    }
  }

  function onDragEnd(event: DragEndEvent) {
    if (disabled) {
      dragging = false;
      return;
    }

    if (event.canceled) {
      previewItems = [...dragSnapshot];
      dragging = false;
      return;
    }

    const nextIds = activeIds;

    if (!sameSortableIds(nextIds, propIds)) {
      const result = onReorder(nextIds);

      if (isPromiseLike(result)) {
        void result
          .catch(() => undefined)
          .finally(() => {
            if (!sameSortableIds(propIds, nextIds)) {
              previewItems = null;
            }
          });
      }
    }

    dragging = false;
  }

  function isPromiseLike(value: void | Promise<void>): value is Promise<void> {
    return typeof value === 'object' && value !== null && typeof value.then === 'function';
  }
</script>

<DragDropProvider {onDragStart} {onDragOver} {onDragEnd}>
  <div {...rest} class={cn(className)}>
    {#each activeItems as item, index (sortableId(item))}
      <SortableListItem id={sortableId(item)} {index} {group} {disabled} element={itemElement} class={itemClass}>
        {#snippet children(controls)}
          {@render renderItem(item, index, controls)}
        {/snippet}
      </SortableListItem>
    {/each}
  </div>
</DragDropProvider>
