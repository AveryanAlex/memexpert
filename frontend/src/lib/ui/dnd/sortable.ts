import type { SortableId, SortableItem } from './types';

export function sortableId(item: SortableItem): SortableId {
  return typeof item === 'string' ? item : item.id;
}

export function sortableIds<Item extends SortableItem>(items: readonly Item[]): SortableId[] {
  return items.map(sortableId);
}

export function sameSortableIds(a: readonly SortableId[], b: readonly SortableId[]): boolean {
  return a.length === b.length && a.every((id, index) => id === b[index]);
}

export function moveSortableIdByIndex(ids: readonly SortableId[], fromIndex: number, toIndex: number): SortableId[] {
  if (fromIndex === toIndex || !isValidIndex(ids, fromIndex) || !isValidIndex(ids, toIndex)) {
    return [...ids];
  }

  const next = [...ids];
  const [moved] = next.splice(fromIndex, 1);
  next.splice(toIndex, 0, moved);
  return next;
}

export function orderSortableItemsByIds<Item extends SortableItem>(items: readonly Item[], ids: readonly SortableId[]): Item[] {
  const byId = new Map(items.map((item) => [sortableId(item), item]));
  const ordered = ids.flatMap((id) => {
    const item = byId.get(id);
    return item ? [item] : [];
  });
  const orderedIds = new Set(ordered.map(sortableId));
  return [...ordered, ...items.filter((item) => !orderedIds.has(sortableId(item)))];
}

function isValidIndex(ids: readonly SortableId[], index: number): boolean {
  return Number.isInteger(index) && index >= 0 && index < ids.length;
}
