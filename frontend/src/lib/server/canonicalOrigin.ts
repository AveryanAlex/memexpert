import { env } from '$env/dynamic/private';

export const DEFAULT_PUBLIC_ORIGIN = 'https://memexpert.net';

type CanonicalOriginEnv = Record<string, string | undefined>;

export function canonicalPublicOrigin(source: CanonicalOriginEnv = env): string {
  return normalizePublicOrigin(source.FRONTEND_ORIGIN) ?? normalizePublicOrigin(source.ORIGIN) ?? DEFAULT_PUBLIC_ORIGIN;
}

export function normalizePublicOrigin(value: string | undefined): string | null {
  const trimmed = value?.trim().replace(/\/+$/, '');
  if (!trimmed) {
    return null;
  }

  try {
    const url = new URL(trimmed);
    if (url.protocol === 'http:' || url.protocol === 'https:') {
      return url.origin;
    }
  } catch {
    return null;
  }

  return null;
}
