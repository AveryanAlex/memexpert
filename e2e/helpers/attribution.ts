import { expect, type Request } from '@playwright/test';

export interface ExpectedMemeAttribution {
  token: string;
}

export function expectedAttributionFromHref(href: string, baseUrl: string): ExpectedMemeAttribution {
  const url = new URL(href, baseUrl);
  return { token: requiredSearchParam(url, 'attribution_token') };
}

export function expectRequestAttribution(request: Request, expected: ExpectedMemeAttribution, context: string) {
  const postData = request.postData();
  if (!postData) throw new Error(`Expected ${request.url()} to include ${context} attribution JSON.`);

  let payload: {
    attribution_token?: string;
    events?: Array<{ attribution_token?: string }>;
  };
  try {
    payload = JSON.parse(postData) as {
      attribution_token?: string;
      events?: Array<{ attribution_token?: string }>;
    };
  } catch (error) {
    throw new Error(`Expected ${request.url()} to include valid ${context} attribution JSON.`, { cause: error });
  }

  const tokens = [payload.attribution_token, ...(payload.events ?? []).map((event) => event.attribution_token)];
  expect(tokens, `Expected ${request.url()} ${context} signed attribution token`).toContain(expected.token);
}

function requiredSearchParam(url: URL, key: string): string {
  const value = url.searchParams.get(key);
  if (value === null || value === '') {
    throw new Error(`Expected ${key} on attributed meme detail link ${url.toString()}.`);
  }
  return value;
}
