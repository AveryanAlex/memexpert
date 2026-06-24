import { describe, expect, it } from 'vitest';
import type { CurrentSessionRead, TelegramLinkStartRead } from '$lib/api/types';
import { buildTelegramStartCommand, isFullSession, LOGIN_PROVIDER_OPTIONS, telegramExpiryLabel } from './telegram-login';

describe('telegram-login helpers', () => {
  it('builds the manual bot command using the backend link_ start prefix', () => {
    expect(buildTelegramStartCommand({ code: 'abc_123' } as TelegramLinkStartRead)).toBe('/start link_abc_123');
  });

  it('formats expiry labels in minutes and seconds', () => {
    expect(telegramExpiryLabel({ expires_in_seconds: 600 } as TelegramLinkStartRead)).toBe('Expires in about 10 minutes');
    expect(telegramExpiryLabel({ expires_in_seconds: 45 } as TelegramLinkStartRead)).toBe('Expires in less than 1 minute');
  });

  it('detects full sessions after Telegram redemption or merge repair', () => {
    expect(isFullSession(null)).toBe(false);
    expect(isFullSession({ user: { account_type: 'guest' } } as CurrentSessionRead)).toBe(false);
    expect(isFullSession({ user: { account_type: 'full' } } as CurrentSessionRead)).toBe(true);
  });

  it('keeps Telegram active while Google and email are marked as later providers', () => {
    expect(LOGIN_PROVIDER_OPTIONS).toMatchObject([
      { id: 'telegram', label: 'Telegram', status: 'available' },
      { id: 'google', label: 'Google', status: 'coming_later' },
      { id: 'email', label: 'Email', status: 'coming_later' }
    ]);
  });
});
