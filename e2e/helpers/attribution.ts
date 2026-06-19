export interface ExpectedMemeAttribution {
  requestId: string;
  impressionId: string;
  sourceAlgorithm: string;
  surface?: string;
  rank?: string;
}

export function expectedAttributionFromHref(href: string, baseUrl: string): ExpectedMemeAttribution {
  const url = new URL(href, baseUrl);
  return {
    requestId: requiredSearchParam(url, 'attribution_request_id'),
    impressionId: requiredSearchParam(url, 'attribution_impression_id'),
    sourceAlgorithm: requiredSearchParam(url, 'attribution_source_algorithm'),
    surface: url.searchParams.get('attribution_surface') ?? undefined,
    rank: url.searchParams.get('attribution_rank') ?? undefined
  };
}

function requiredSearchParam(url: URL, key: string): string {
  const value = url.searchParams.get(key);
  if (!value) {
    throw new Error(`Expected ${key} on attributed meme detail link ${url.toString()}.`);
  }
  return value;
}
