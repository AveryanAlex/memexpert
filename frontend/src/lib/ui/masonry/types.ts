export type MasonryKey = string | number;

export type MasonryElement = 'div' | 'main' | 'section';

export interface MasonryItemLayout {
  columnCount: number;
  ready: boolean;
}

export interface MasonryPosition {
  index: number;
  column: number;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface MasonryLayout {
  columnCount: number;
  columnWidth: number;
  height: number;
  positions: MasonryPosition[];
}

export interface MasonryLayoutOptions {
  containerWidth: number;
  itemHeights: readonly number[];
  minColumnWidth?: number;
  gap?: number;
  maxColumns?: number;
}
