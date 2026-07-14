import {
  ApiError,
  addAdminTelegramChannelFromReference,
  type ApiFetch
} from '$lib/api/client';
import type { AdminSourceChannelRead, AdminTelegramChannelFromReferencePayload } from '$lib/api/types';

export const SOURCE_ADD_RETRY_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 12_000] as const;

interface AddTelegramSourceWithRetryOptions {
  fetch: ApiFetch;
  baseUrl: string;
  body: AdminTelegramChannelFromReferencePayload;
  retryDelaysMs?: readonly number[];
  wait?: (delayMs: number) => Promise<void>;
  onRetry?: (retryNumber: number, delayMs: number) => void;
}

export async function addTelegramSourceWithRetry({
  fetch,
  baseUrl,
  body,
  retryDelaysMs = SOURCE_ADD_RETRY_DELAYS_MS,
  wait = waitForDelay,
  onRetry
}: AddTelegramSourceWithRetryOptions): Promise<AdminSourceChannelRead> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await addAdminTelegramChannelFromReference({ fetch, baseUrl, body });
    } catch (error) {
      const retryDelay = retryDelaysMs[attempt];
      if (retryDelay === undefined || !isRetryableSourceAddError(error)) throw error;

      onRetry?.(attempt + 1, retryDelay);
      await wait(retryDelay);
    }
  }
}

export function isRetryableSourceAddError(error: unknown): boolean {
  return error instanceof TypeError || (error instanceof ApiError && [502, 503, 504].includes(error.status));
}

function waitForDelay(delayMs: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, delayMs));
}
