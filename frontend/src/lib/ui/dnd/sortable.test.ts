import { describe, expect, it } from 'vitest';

import { moveSortableIdByIndex, orderSortableItemsByIds, sameSortableIds, sortableIds } from './sortable';

describe('shared sortable helpers', () => {
  it('normalizes string ids and item ids', () => {
    expect(sortableIds(['a', { id: 'b' }, 'c'])).toEqual(['a', 'b', 'c']);
  });

  it('moves ids by sortable indexes without mutating the source array', () => {
    const ids = ['a', 'b', 'c'];

    expect(moveSortableIdByIndex(ids, 2, 0)).toEqual(['c', 'a', 'b']);
    expect(ids).toEqual(['a', 'b', 'c']);
  });

  it('preserves ids when sortable indexes are invalid or unchanged', () => {
    expect(moveSortableIdByIndex(['a', 'b'], 0, 0)).toEqual(['a', 'b']);
    expect(moveSortableIdByIndex(['a', 'b'], -1, 1)).toEqual(['a', 'b']);
    expect(moveSortableIdByIndex(['a', 'b'], 0, 2)).toEqual(['a', 'b']);
  });

  it('orders items by ids and appends missing items in source order', () => {
    const items = [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }, { id: 'c', label: 'C' }];

    expect(orderSortableItemsByIds(items, ['c', 'a']).map((item) => item.id)).toEqual(['c', 'a', 'b']);
  });

  it('compares sortable id sequences by position', () => {
    expect(sameSortableIds(['a', 'b'], ['a', 'b'])).toBe(true);
    expect(sameSortableIds(['a', 'b'], ['b', 'a'])).toBe(false);
    expect(sameSortableIds(['a'], ['a', 'b'])).toBe(false);
  });
});
