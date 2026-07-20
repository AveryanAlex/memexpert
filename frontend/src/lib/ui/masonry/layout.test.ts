import { describe, expect, it } from 'vitest';

import { calculateMasonryLayout, masonryColumnCount } from './layout';

describe('measured masonry layout', () => {
  it('selects one through four columns from container width', () => {
    expect(masonryColumnCount(279)).toBe(1);
    expect(masonryColumnCount(576)).toBe(2);
    expect(masonryColumnCount(872)).toBe(3);
    expect(masonryColumnCount(1168)).toBe(4);
    expect(masonryColumnCount(1800)).toBe(4);
    expect(masonryColumnCount(1800, 280, 16, 3)).toBe(3);
  });

  it('places ranked items in the shortest column with monotonic tops', () => {
    const layout = calculateMasonryLayout({
      containerWidth: 900,
      itemHeights: [300, 100, 200, 50, 60, 70]
    });

    expect(layout.columnCount).toBe(3);
    expect(layout.positions.map(({ column, y }) => ({ column, y }))).toEqual([
      { column: 0, y: 0 },
      { column: 1, y: 0 },
      { column: 2, y: 0 },
      { column: 1, y: 116 },
      { column: 1, y: 182 },
      { column: 2, y: 216 }
    ]);

    const tops = layout.positions.map((position) => position.y);
    expect(tops).toEqual([...tops].sort((left, right) => left - right));
  });

  it('breaks equal-height column ties toward the left', () => {
    const layout = calculateMasonryLayout({
      containerWidth: 900,
      itemHeights: [100, 100, 100, 50]
    });

    expect(layout.positions[3]).toMatchObject({ column: 0, y: 116 });
  });

  it('uses measured heights without same-column overlap and reports the exact container height', () => {
    const gap = 12;
    const layout = calculateMasonryLayout({
      containerWidth: 650,
      itemHeights: [400, 80, 90, 200, 50, 175],
      minColumnWidth: 200,
      gap,
      maxColumns: 3
    });

    for (const position of layout.positions) {
      const laterInColumn = layout.positions.filter(
        (candidate) => candidate.column === position.column && candidate.index > position.index
      );
      for (const later of laterInColumn) {
        expect(later.y).toBeGreaterThanOrEqual(position.y + position.height + gap);
      }
    }

    expect(layout.height).toBe(Math.max(...layout.positions.map((position) => position.y + position.height)));
  });

  it('handles empty and unusable measurements deterministically', () => {
    expect(calculateMasonryLayout({ containerWidth: 0, itemHeights: [] })).toEqual({
      columnCount: 1,
      columnWidth: 0,
      height: 0,
      positions: []
    });

    const layout = calculateMasonryLayout({
      containerWidth: 600,
      itemHeights: [0, Number.NaN, -20, 40]
    });

    expect(layout.positions.map((position) => position.height)).toEqual([0, 0, 0, 40]);
    expect(layout.positions.every(({ x, y, width, height }) => [x, y, width, height].every(Number.isFinite))).toBe(true);
  });

  it('recalculates widths and placement when the container changes columns', () => {
    const narrow = calculateMasonryLayout({ containerWidth: 575, itemHeights: [100, 200, 50] });
    const wide = calculateMasonryLayout({ containerWidth: 900, itemHeights: [100, 200, 50] });

    expect(narrow.columnCount).toBe(1);
    expect(narrow.positions.map((position) => position.y)).toEqual([0, 116, 332]);
    expect(wide.columnCount).toBe(3);
    expect(wide.positions.map((position) => position.y)).toEqual([0, 0, 0]);
    expect(wide.columnWidth).toBeLessThan(narrow.columnWidth);
  });
});
