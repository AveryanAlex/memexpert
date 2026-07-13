export const SOURCE_POST_PAGE_SIZE = 50;
export const MAX_SOURCE_POST_PAGE = Math.floor(Number.MAX_SAFE_INTEGER / SOURCE_POST_PAGE_SIZE) + 1;

export function sourcePostPageFromSearchParam(value: string | null): number {
  if (!value || !/^\d+$/.test(value)) return 1;
  const page = Number(value);
  if (!Number.isFinite(page) || page > MAX_SOURCE_POST_PAGE) return MAX_SOURCE_POST_PAGE;
  if (!Number.isSafeInteger(page) || page < 1) return 1;
  return page;
}
