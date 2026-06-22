import { describe, expect, it, vi } from 'vitest';

import {
  extractTelegramMiniAppBootstrapState,
  extractTelegramThemeVariables,
  extractTelegramViewportVariables,
  hasTelegramMiniAppBootstrapData,
  mapTelegramStartParamToRoute,
  normalizeTelegramWebApp,
  readTelegramWebApp,
  telegramWebAppFromLaunchParams,
  telegramLaunchParamsFromUrl
} from './telegram-miniapp';

describe('Telegram Mini App helpers', () => {
  it('normalizes WebApp-like objects and preserves callable host methods', () => {
    const ready = vi.fn(function (this: { marker: string }) {
      expect(this.marker).toBe('webapp');
    });
    const expand = vi.fn(function (this: { isExpanded: boolean }) {
      this.isExpanded = true;
    });
    const webAppLike = {
      marker: 'webapp',
      initData: 'query_id=abc',
      initDataUnsafe: { start_param: 'meme_slug' },
      themeParams: { bg_color: '#101820' },
      colorScheme: 'dark',
      viewportHeight: 640,
      viewportStableHeight: 620,
      isExpanded: false,
      ready,
      expand
    };

    const normalized = normalizeTelegramWebApp(webAppLike);

    expect(normalized?.initData).toBe('query_id=abc');
    expect(normalized?.colorScheme).toBe('dark');
    normalized?.ready?.();
    normalized?.expand?.();
    expect(ready).toHaveBeenCalledOnce();
    expect(expand).toHaveBeenCalledOnce();
    expect(webAppLike.isExpanded).toBe(true);
  });

  it('reads WebApp from window-like Telegram globals only when present', () => {
    expect(readTelegramWebApp({})).toBeNull();

    const webApp = readTelegramWebApp({
      Telegram: {
        WebApp: {
          initData: 'user=%7B%7D',
          initDataUnsafe: null,
          themeParams: {},
          colorScheme: 'light'
        }
      }
    });

    expect(webApp?.initData).toBe('user=%7B%7D');
    expect(webApp?.colorScheme).toBe('light');
  });

  it('maps invite and meme start payloads to existing routes with URL encoding', () => {
    expect(mapTelegramStartParamToRoute('invite_abc123')).toBe('/collection/invite/abc123');
    expect(mapTelegramStartParamToRoute('meme_smoke-test-cat-reaction')).toBe('/memes/smoke-test-cat-reaction');
    expect(mapTelegramStartParamToRoute('meme_slug/with spaces')).toBe('/memes/slug%2Fwith%20spaces');
    expect(mapTelegramStartParamToRoute('invite_')).toBeNull();
    expect(mapTelegramStartParamToRoute('profile')).toBeNull();
    expect(mapTelegramStartParamToRoute(null)).toBeNull();
  });

  it('extracts bootstrap state from initDataUnsafe start params first', () => {
    const state = extractTelegramMiniAppBootstrapState(
      {
        initData: 'start_param=invite_from_init_data',
        initDataUnsafe: { start_param: 'meme_from_unsafe' },
        themeParams: {
          bg_color: '#111827',
          button_color: '#f97316',
          text_color: '#f9fafb',
          unsupported_object: { nope: true }
        },
        colorScheme: 'dark',
        viewportHeight: 700,
        viewportStableHeight: 680,
        isExpanded: true
      },
      new URLSearchParams({ tgWebAppStartParam: 'invite_from_launch_params' })
    );

    expect(state).toEqual({
      initData: 'start_param=invite_from_init_data',
      startParam: 'meme_from_unsafe',
      startRoute: '/memes/from_unsafe',
      dataAttributes: {
        'data-telegram-miniapp': 'true',
        'data-telegram-color-scheme': 'dark',
        'data-telegram-expanded': 'true'
      },
      classNames: ['telegram-miniapp'],
      cssVariables: {
        '--tg-theme-bg-color': '#111827',
        '--tg-theme-button-color': '#f97316',
        '--tg-theme-text-color': '#f9fafb',
        '--tg-viewport-height': '700px',
        '--tg-viewport-stable-height': '680px'
      }
    });
  });

  it('falls back to launch and initData start params without crashing on unknown payloads', () => {
    const launchState = extractTelegramMiniAppBootstrapState(
      {
        initData: '',
        initDataUnsafe: {},
        themeParams: {},
        isExpanded: false
      },
      new URLSearchParams({ tgWebAppStartParam: 'invite_launch-token' })
    );
    const initDataState = extractTelegramMiniAppBootstrapState({
      initData: 'start_param=meme_query-slug',
      initDataUnsafe: null,
      themeParams: {}
    });
    const unknownState = extractTelegramMiniAppBootstrapState({
      initData: 'start_param=collections',
      initDataUnsafe: null,
      themeParams: {}
    });

    expect(launchState?.startRoute).toBe('/collection/invite/launch-token');
    expect(initDataState?.startRoute).toBe('/memes/query-slug');
    expect(unknownState?.startParam).toBe('collections');
    expect(unknownState?.startRoute).toBeNull();
  });

  it('builds a WebApp-like state from Telegram launch params when the host script is unavailable', () => {
    const initData = 'query_id=abc123&user=%7B%22id%22%3A303030303%7D&auth_date=1770000000&hash=signed';
    const launchParams = telegramLaunchParamsFromUrl(
      `https://app.test/#tgWebAppData=${encodeURIComponent(initData)}&tgWebAppStartParam=meme_hash-slug&tgWebAppThemeParams=${encodeURIComponent(
        JSON.stringify({ bg_color: '#101820', text_color: '#f8fafc' })
      )}&tgWebAppColorScheme=dark`
    );

    const webApp = telegramWebAppFromLaunchParams(launchParams);
    const state = extractTelegramMiniAppBootstrapState(webApp, launchParams);

    expect(webApp?.initData).toBe(initData);
    expect(webApp?.initDataUnsafe).toEqual({ start_param: 'meme_hash-slug' });
    expect(state).toMatchObject({
      initData,
      startParam: 'meme_hash-slug',
      startRoute: '/memes/hash-slug',
      dataAttributes: {
        'data-telegram-miniapp': 'true',
        'data-telegram-color-scheme': 'dark'
      },
      cssVariables: {
        '--tg-theme-bg-color': '#101820',
        '--tg-theme-text-color': '#f8fafc'
      }
    });
  });

  it('returns no launch-param WebApp fallback for non-Telegram URLs', () => {
    expect(telegramWebAppFromLaunchParams(telegramLaunchParamsFromUrl('https://app.test/?foo=bar'))).toBeNull();
    expect(telegramWebAppFromLaunchParams(new URLSearchParams({ tgWebAppColorScheme: 'dark' }))).toBeNull();
  });

  it('distinguishes an empty Telegram SDK object from a real Mini App launch', () => {
    expect(
      hasTelegramMiniAppBootstrapData(
        normalizeTelegramWebApp({
          initData: '',
          initDataUnsafe: {},
          themeParams: {},
          colorScheme: 'light',
          isExpanded: true
        })
      )
    ).toBe(false);

    expect(
      hasTelegramMiniAppBootstrapData(
        normalizeTelegramWebApp({
          initData: 'auth_date=1770000000&hash=signed',
          initDataUnsafe: {},
          themeParams: {},
          colorScheme: 'light'
        })
      )
    ).toBe(true);
  });

  it('extracts theme and viewport CSS variables from safe primitive values', () => {
    expect(
      extractTelegramThemeVariables({
        bg_color: '#0f172a',
        hint_color: '#94a3b8',
        text_color: '',
        section_bg_color: 123
      })
    ).toEqual({
      '--tg-theme-bg-color': '#0f172a',
      '--tg-theme-hint-color': '#94a3b8'
    });
    expect(extractTelegramViewportVariables({ viewportHeight: 0, viewportStableHeight: 720 })).toEqual({
      '--tg-viewport-stable-height': '720px'
    });
  });

  it('merges Telegram launch params from query and hash fragments', () => {
    const params = telegramLaunchParamsFromUrl('https://app.test/?foo=bar#tgWebAppStartParam=meme_hash-slug&foo=ignored');

    expect(params.get('foo')).toBe('bar');
    expect(params.get('tgWebAppStartParam')).toBe('meme_hash-slug');
  });
});
