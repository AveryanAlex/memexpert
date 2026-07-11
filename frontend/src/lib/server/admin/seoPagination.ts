export const SEO_REVIEW_PAGE_SIZE = 25;
export const MAX_SEO_REVIEW_PAGE = Math.floor(Number.MAX_SAFE_INTEGER / SEO_REVIEW_PAGE_SIZE) + 1;

export function seoReviewPageFromSearchParam(value: string | null): number {
  if (!value || !/^\d+$/.test(value)) return 1;
  const page = Number(value);
  if (!Number.isFinite(page) || page > MAX_SEO_REVIEW_PAGE) return MAX_SEO_REVIEW_PAGE;
  if (!Number.isSafeInteger(page) || page < 1) return 1;
  return page;
}
