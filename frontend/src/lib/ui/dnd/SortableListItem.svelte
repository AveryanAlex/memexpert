<script lang="ts">
  import { createSortable } from '@dnd-kit/svelte/sortable';
  import type { Snippet } from 'svelte';
  import type { HTMLAttributes } from 'svelte/elements';
  import { cn } from '../styles';
  import type { SortableId, SortableItemControls } from './types';

  type SortableElement = 'article' | 'div' | 'li' | 'section';

  interface Props extends Omit<HTMLAttributes<HTMLElement>, 'children'> {
    id: SortableId;
    index: number;
    group?: SortableId;
    disabled?: boolean;
    element?: SortableElement;
    children?: Snippet<[SortableItemControls]>;
  }

  let {
    id,
    index,
    group,
    disabled = false,
    element = 'div',
    class: className = '',
    children,
    ...rest
  }: Props = $props();

  const sortable = createSortable({
    get id() {
      return id;
    },
    get index() {
      return index;
    },
    get group() {
      return group;
    },
    get disabled() {
      return disabled;
    }
  });

  const controls: SortableItemControls = {
    attachHandle: (node) => sortable.attachHandle(node),
    get isDragging() {
      return sortable.isDragging;
    },
    get isDropping() {
      return sortable.isDropping;
    },
    get isDragSource() {
      return sortable.isDragSource;
    },
    get isDropTarget() {
      return sortable.isDropTarget;
    }
  };
</script>

<svelte:element
  this={element}
  {...rest}
  {@attach sortable.attach}
  class={cn(
    className,
    sortable.isDragSource && 'opacity-70',
    sortable.isDropTarget && !sortable.isDragSource && 'ring-2 ring-ink/20'
  )}
  data-dnd-sortable="true"
  data-dnd-drag-source={sortable.isDragSource ? 'true' : undefined}
  data-dnd-drop-target={sortable.isDropTarget ? 'true' : undefined}
>
  {#if children}{@render children(controls)}{/if}
</svelte:element>
