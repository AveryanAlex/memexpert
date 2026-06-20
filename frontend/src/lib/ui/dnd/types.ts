export type SortableId = string;

export type SortableItem = SortableId | { id: SortableId };

export type SortableAttach = (node: HTMLElement) => () => void;

export interface SortableItemControls {
  attachHandle: SortableAttach;
  isDragging: boolean;
  isDropping: boolean;
  isDragSource: boolean;
  isDropTarget: boolean;
}
