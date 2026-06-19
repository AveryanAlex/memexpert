import { expect, type Request } from '@playwright/test';

export interface ExpectedMemeAttribution {
  requestId: string;
  impressionId: string;
  sourceAlgorithm: string;
  surface: string;
  rank: number;
}

export function expectedAttributionFromHref(href: string, baseUrl: string): ExpectedMemeAttribution {
  const url = new URL(href, baseUrl);
  return {
    requestId: requiredSearchParam(url, 'attribution_request_id'),
    impressionId: requiredSearchParam(url, 'attribution_impression_id'),
    sourceAlgorithm: requiredSearchParam(url, 'attribution_source_algorithm'),
    surface: requiredSearchParam(url, 'attribution_surface'),
    rank: requiredIntegerSearchParam(url, 'attribution_rank')
  };
}

export function expectRequestAttribution(request: Request, expected: ExpectedMemeAttribution, context: string) {
  const postData = request.postData();
  if (!postData) throw new Error(`Expected ${request.url()} to include ${context} attribution JSON.`);

  let payload: { attribution?: Record<string, unknown> };
  try {
    payload = JSON.parse(postData) as { attribution?: Record<string, unknown> };
  } catch (error) {
    throw new Error(`Expected ${request.url()} to include valid ${context} attribution JSON.`, { cause: error });
  }

  expect(payload.attribution, `Expected ${request.url()} ${context} attribution payload`).toEqual(
    expect.objectContaining({
      request_id: expected.requestId,
      impression_id: expected.impressionId,
      source_algorithm: expected.sourceAlgorithm,
      surface: expected.surface,
      rank: expected.rank
    })
  );
}

function requiredSearchParam(url: URL, key: string): string {
  const value = url.searchParams.get(key);
  if (value === null || value === '') {
    throw new Error(`Expected ${key} on attributed meme detail link ${url.toString()}.`);
  }
  return value;
}

function requiredIntegerSearchParam(url: URL, key: string): number {
  const raw = requiredSearchParam(url, key);
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(`Expected integer ${key} on attributed meme detail link ${url.toString()}.`);
  }
  return value;
}
