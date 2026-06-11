import { env } from '$env/dynamic/private';
import type { Cookies } from '@sveltejs/kit';

export const ACCESS_COOKIE_NAME = 'memexpert_access_token';

export function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}

export function forwardBackendAccessCookie(response: Response, cookies: Cookies): string | null {
  const setCookie = response.headers.get('set-cookie');
  if (!setCookie) {
    return null;
  }

  const parsed = parseAccessCookie(setCookie);
  if (!parsed) {
    return null;
  }

  cookies.set(ACCESS_COOKIE_NAME, parsed.value, {
    path: parsed.path ?? '/',
    httpOnly: parsed.httpOnly,
    secure: parsed.secure,
    sameSite: parsed.sameSite,
    maxAge: parsed.maxAge,
    expires: parsed.expires
  });

  return parsed.value;
}

export function cookieHeaderWithAccessToken(cookieHeader: string | undefined, accessToken: string | null): string | undefined {
  if (!accessToken) {
    return cookieHeader;
  }

  const withoutAccess = (cookieHeader ?? '')
    .split(';')
    .map((part) => part.trim())
    .filter((part) => part && !part.startsWith(`${ACCESS_COOKIE_NAME}=`));

  return [`${ACCESS_COOKIE_NAME}=${accessToken}`, ...withoutAccess].join('; ');
}

interface ParsedAccessCookie {
  value: string;
  path: string | undefined;
  httpOnly: boolean;
  secure: boolean;
  sameSite: 'lax' | 'strict' | 'none';
  maxAge: number | undefined;
  expires: Date | undefined;
}

function parseAccessCookie(setCookie: string): ParsedAccessCookie | null {
  const parts = setCookie.split(';').map((part) => part.trim());
  const [nameValue, ...attributes] = parts;
  const separatorIndex = nameValue.indexOf('=');
  if (separatorIndex <= 0 || nameValue.slice(0, separatorIndex) !== ACCESS_COOKIE_NAME) {
    return null;
  }

  const parsed: ParsedAccessCookie = {
    value: unquoteCookieValue(nameValue.slice(separatorIndex + 1)),
    path: '/',
    httpOnly: true,
    secure: false,
    sameSite: 'lax',
    maxAge: undefined,
    expires: undefined
  };

  for (const attribute of attributes) {
    const [rawName, ...rawValueParts] = attribute.split('=');
    const name = rawName.toLowerCase();
    const value = rawValueParts.join('=');

    if (name === 'path' && value) {
      parsed.path = value;
    } else if (name === 'httponly') {
      parsed.httpOnly = true;
    } else if (name === 'secure') {
      parsed.secure = true;
    } else if (name === 'samesite') {
      parsed.sameSite = normalizeSameSite(value);
    } else if (name === 'max-age') {
      const maxAge = Number.parseInt(value, 10);
      parsed.maxAge = Number.isFinite(maxAge) ? maxAge : undefined;
    } else if (name === 'expires') {
      const expires = new Date(value);
      parsed.expires = Number.isNaN(expires.getTime()) ? undefined : expires;
    }
  }

  return parsed;
}

function normalizeSameSite(value: string): 'lax' | 'strict' | 'none' {
  const normalized = value.toLowerCase();
  if (normalized === 'strict' || normalized === 'none') {
    return normalized;
  }
  return 'lax';
}

function unquoteCookieValue(value: string): string {
  if (value.startsWith('"') && value.endsWith('"')) {
    return value.slice(1, -1);
  }
  return value;
}
