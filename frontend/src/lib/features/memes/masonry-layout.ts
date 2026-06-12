import type { PublicMemeCardRead } from "$lib/api/types";

export interface MasonryColumn<T> {
  id: string;
  items: T[];
  estimatedHeight: number;
}

const MIN_COLUMN_WIDTH_PX = 280;
const MAX_COLUMNS = 4;
const COLUMN_GAP_PX = 16;
const DEFAULT_MEDIA_RATIO = 4 / 3;
const MIN_MEDIA_HEIGHT_PX = 152;
const MAX_MEDIA_HEIGHT_PX = 420;

export function masonryColumnCount(containerWidth: number): number {
  if (!Number.isFinite(containerWidth) || containerWidth <= 0) return 1;

  const count = Math.floor(
    (containerWidth + COLUMN_GAP_PX) / (MIN_COLUMN_WIDTH_PX + COLUMN_GAP_PX),
  );
  return clamp(count, 1, MAX_COLUMNS);
}

export function masonryColumnWidth(
  containerWidth: number,
  columnCount: number,
): number {
  const safeColumnCount = clamp(Math.trunc(columnCount), 1, MAX_COLUMNS);
  if (!Number.isFinite(containerWidth) || containerWidth <= 0)
    return MIN_COLUMN_WIDTH_PX;

  return Math.max(
    MIN_COLUMN_WIDTH_PX,
    (containerWidth - COLUMN_GAP_PX * (safeColumnCount - 1)) / safeColumnCount,
  );
}

export function buildMasonryColumns<T extends PublicMemeCardRead>(
  items: T[],
  columnCount: number,
  columnWidth = MIN_COLUMN_WIDTH_PX,
): MasonryColumn<T>[] {
  const columns = Array.from(
    { length: clamp(Math.trunc(columnCount), 1, MAX_COLUMNS) },
    (_, index) => ({
      id: `masonry-column-${index}`,
      items: [] as T[],
      estimatedHeight: 0,
    }),
  );

  for (const item of items) {
    const column = shortestColumn(columns);
    column.items.push(item);
    column.estimatedHeight +=
      estimatedCardHeight(item, columnWidth) + COLUMN_GAP_PX;
  }

  return columns;
}

export function estimatedCardHeight(
  meme: PublicMemeCardRead,
  columnWidth = MIN_COLUMN_WIDTH_PX,
): number {
  const file = meme.primary_file;
  const width = file?.render?.width ?? file?.width ?? null;
  const height = file?.render?.height ?? file?.height ?? null;
  const aspectRatio = width && height ? width / height : DEFAULT_MEDIA_RATIO;
  const mediaHeight = clamp(
    columnWidth / aspectRatio,
    MIN_MEDIA_HEIGHT_PX,
    MAX_MEDIA_HEIGHT_PX,
  );
  const tagRows = Math.ceil(Math.min(meme.tags.length, 3) / 3);
  const captionRows = Math.max(1, Math.ceil((meme.caption?.length ?? 24) / 34));

  return mediaHeight + 90 + tagRows * 34 + captionRows * 12;
}

function shortestColumn<T>(columns: MasonryColumn<T>[]): MasonryColumn<T> {
  return columns.reduce(
    (best, column) =>
      column.estimatedHeight < best.estimatedHeight ? column : best,
    columns[0],
  );
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
