import type { MasonryLayout, MasonryLayoutOptions } from './types';

export const DEFAULT_MASONRY_MIN_COLUMN_WIDTH = 280;
export const DEFAULT_MASONRY_GAP = 16;
export const DEFAULT_MASONRY_MAX_COLUMNS = 4;

export function masonryColumnCount(
  containerWidth: number,
  minColumnWidth = DEFAULT_MASONRY_MIN_COLUMN_WIDTH,
  gap = DEFAULT_MASONRY_GAP,
  maxColumns = DEFAULT_MASONRY_MAX_COLUMNS
): number {
  const width = nonNegativeFinite(containerWidth);
  const minimum = positiveFinite(minColumnWidth, DEFAULT_MASONRY_MIN_COLUMN_WIDTH);
  const spacing = nonNegativeFinite(gap);
  const maximum = positiveInteger(maxColumns, DEFAULT_MASONRY_MAX_COLUMNS);

  if (width === 0) return 1;

  const fittingColumns = Math.floor((width + spacing) / (minimum + spacing));
  return Math.min(maximum, Math.max(1, fittingColumns));
}

export function calculateMasonryLayout({
  containerWidth,
  itemHeights,
  minColumnWidth = DEFAULT_MASONRY_MIN_COLUMN_WIDTH,
  gap = DEFAULT_MASONRY_GAP,
  maxColumns = DEFAULT_MASONRY_MAX_COLUMNS
}: MasonryLayoutOptions): MasonryLayout {
  const width = nonNegativeFinite(containerWidth);
  const spacing = nonNegativeFinite(gap);
  const columnCount = masonryColumnCount(width, minColumnWidth, spacing, maxColumns);
  const columnWidth = Math.max(0, (width - spacing * (columnCount - 1)) / columnCount);
  const columnBottoms = Array.from({ length: columnCount }, () => 0);
  const positions = [];
  let previousTop = 0;
  let height = 0;

  for (const [index, rawHeight] of itemHeights.entries()) {
    const itemHeight = nonNegativeFinite(rawHeight);
    let column = 0;

    for (let candidate = 1; candidate < columnCount; candidate += 1) {
      if (columnBottoms[candidate] < columnBottoms[column]) {
        column = candidate;
      }
    }

    // Choosing the current shortest column naturally produces non-decreasing tops. Keep the
    // explicit bound so the rank invariant also survives malformed or zero-height measurements.
    const y = Math.max(columnBottoms[column], previousTop);
    const itemBottom = y + itemHeight;

    positions.push({
      index,
      column,
      x: column * (columnWidth + spacing),
      y,
      width: columnWidth,
      height: itemHeight
    });

    previousTop = y;
    columnBottoms[column] = itemBottom + spacing;
    height = Math.max(height, itemBottom);
  }

  return { columnCount, columnWidth, height, positions };
}

function nonNegativeFinite(value: number): number {
  return Number.isFinite(value) ? Math.max(0, value) : 0;
}

function positiveFinite(value: number, fallback: number): number {
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function positiveInteger(value: number, fallback: number): number {
  return Number.isFinite(value) && value >= 1 ? Math.floor(value) : fallback;
}
