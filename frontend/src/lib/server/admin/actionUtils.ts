import { env } from '$env/dynamic/private';
import { fail } from '@sveltejs/kit';
import { ApiError } from '$lib/api/client';
import type { ApiFetch } from '$lib/api/client';

export function apiRequest(fetch: ApiFetch, request: Request) {
  return {
    fetch,
    baseUrl: apiBaseUrl(),
    cookieHeader: request.headers.get('cookie') ?? undefined
  };
}

export async function runAction<T extends { message: string }>(operation: () => Promise<T>) {
  try {
    return await operation();
  } catch (caught) {
    if (caught instanceof ApiError) {
      return fail(caught.status, { message: caught.message, error: true });
    }
    if (caught instanceof Error) {
      return fail(400, { message: caught.message, error: true });
    }
    return fail(500, { message: 'Admin operation failed.', error: true });
  }
}

export function readRequired(data: FormData, name: string): string {
  const value = String(data.get(name) ?? '').trim();
  if (!value) {
    throw new ApiError(400, `${name} is required.`);
  }
  return value;
}

export function readOptional(data: FormData, name: string): string | null {
  const value = String(data.get(name) ?? '').trim();
  return value || null;
}

export function readBoolean(data: FormData, name: string): boolean {
  const value = readRequired(data, name);
  if (value === 'true') return true;
  if (value === 'false') return false;
  throw new ApiError(400, `${name} must be true or false.`);
}

export function readInt(data: FormData, name: string, fallback: number): number {
  const raw = String(data.get(name) ?? '').trim();
  if (!raw) {
    return fallback;
  }
  if (!/^\d+$/.test(raw)) {
    throw new Error(`${name} must be a whole number.`);
  }
  return Number(raw);
}

export function readOptionalInt(data: FormData, name: string): number | null {
  const raw = String(data.get(name) ?? '').trim();
  if (!raw) {
    return null;
  }
  if (!/^\d+$/.test(raw)) {
    throw new Error(`${name} must be a whole number.`);
  }
  return Number(raw);
}

export function requireConfirmation(actual: string, expected: string, message: string): void {
  if (actual !== expected) {
    throw new ApiError(400, message);
  }
}

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
