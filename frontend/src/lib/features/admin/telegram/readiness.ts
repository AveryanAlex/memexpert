import type { AdminTelegramSessionRead } from '$lib/api/types';

/**
 * Whether an account can safely be selected for source assignment or reference lookup.
 *
 * Keep this UI predicate aligned with the safe account requirements for source work;
 * callers still rely on the API to enforce the authoritative server-side policy.
 */
export function isReadyTelegramAccount(account: AdminTelegramSessionRead, now = new Date()): boolean {
  return (
    account.enabled &&
    account.status === 'active' &&
    account.has_string_session &&
    !hasCurrentFloodWait(account.flood_wait_until, now) &&
    account.quarantined_at === null
  );
}

function hasCurrentFloodWait(floodWaitUntil: string | null, now: Date): boolean {
  if (!floodWaitUntil) return false;
  const timestamp = new Date(floodWaitUntil).getTime();
  return !Number.isFinite(timestamp) || timestamp > now.getTime();
}
