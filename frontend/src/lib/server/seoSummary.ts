import { fetchSeoSummary, type ApiFetch } from '$lib/api/client';
import type { SeoCatalogSummaryRead } from '$lib/api/types';

const SEO_SUMMARY_TTL_MS = 5 * 60 * 1000;

const cachedSummaries = new Map<string, { value: SeoCatalogSummaryRead; expiresAt: number }>();
const pendingSummaries = new Map<string, Promise<SeoCatalogSummaryRead>>();

export function fetchCachedSeoSummary(fetch: ApiFetch, baseUrl: string): Promise<SeoCatalogSummaryRead> {
  const cachedSummary = cachedSummaries.get(baseUrl);
  if (cachedSummary && cachedSummary.expiresAt > Date.now()) {
    return Promise.resolve(cachedSummary.value);
  }
  const pendingSummary = pendingSummaries.get(baseUrl);
  if (pendingSummary) {
    return pendingSummary;
  }

  const request = fetchSeoSummary({ fetch, baseUrl }).then((summary) => {
    cachedSummaries.set(baseUrl, { value: summary, expiresAt: Date.now() + SEO_SUMMARY_TTL_MS });
    return summary;
  });
  pendingSummaries.set(baseUrl, request);
  void request.then(
    () => pendingSummaries.delete(baseUrl),
    () => pendingSummaries.delete(baseUrl)
  );
  return request;
}
