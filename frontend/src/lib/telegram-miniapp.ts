export interface TelegramWebAppLike {
  initData: string;
  initDataUnsafe: { start_param?: unknown } | null;
  themeParams: Record<string, unknown>;
  colorScheme: 'dark' | 'light' | null;
  viewportHeight: number | null;
  viewportStableHeight: number | null;
  isExpanded: boolean | null;
  ready?: () => void;
  expand?: () => void;
}

export interface TelegramMiniAppBootstrapState {
  initData: string;
  startParam: string | null;
  startRoute: string | null;
  dataAttributes: Record<string, string>;
  classNames: string[];
  cssVariables: Record<string, string>;
}

const MAX_START_PARAM_LENGTH = 512;

const THEME_PARAM_TO_CSS_VARIABLE: Record<string, string> = {
  accent_text_color: '--tg-theme-accent-text-color',
  bg_color: '--tg-theme-bg-color',
  button_color: '--tg-theme-button-color',
  button_text_color: '--tg-theme-button-text-color',
  destructive_text_color: '--tg-theme-destructive-text-color',
  header_bg_color: '--tg-theme-header-bg-color',
  hint_color: '--tg-theme-hint-color',
  link_color: '--tg-theme-link-color',
  secondary_bg_color: '--tg-theme-secondary-bg-color',
  section_bg_color: '--tg-theme-section-bg-color',
  section_header_text_color: '--tg-theme-section-header-text-color',
  section_separator_color: '--tg-theme-section-separator-color',
  subtitle_text_color: '--tg-theme-subtitle-text-color',
  text_color: '--tg-theme-text-color'
};

export function readTelegramWebApp(source: unknown): TelegramWebAppLike | null {
  if (!isRecord(source)) {
    return null;
  }

  const telegram = source.Telegram;
  if (!isRecord(telegram)) {
    return null;
  }

  return normalizeTelegramWebApp(telegram.WebApp);
}

export function telegramWebAppFromLaunchParams(launchParams: URLSearchParams): TelegramWebAppLike | null {
  const initData = readString(launchParams.get('tgWebAppData')) ?? '';
  const startParam = normalizeStartParam(launchParams.get('tgWebAppStartParam'));
  const themeParams = parseLaunchThemeParams(launchParams.get('tgWebAppThemeParams'));
  const colorScheme = normalizeColorScheme(launchParams.get('tgWebAppColorScheme'));

  if (!initData && !startParam && Object.keys(themeParams).length === 0) {
    return null;
  }

  return {
    initData,
    initDataUnsafe: startParam ? { start_param: startParam } : null,
    themeParams,
    colorScheme,
    viewportHeight: null,
    viewportStableHeight: null,
    isExpanded: null
  };
}

export function hasTelegramMiniAppBootstrapData(webApp: TelegramWebAppLike | null): webApp is TelegramWebAppLike {
  if (!webApp) {
    return false;
  }

  return Boolean(
    webApp.initData.trim() ||
      normalizeStartParam(webApp.initDataUnsafe?.start_param) ||
      Object.keys(webApp.themeParams).length > 0
  );
}

export function normalizeTelegramWebApp(value: unknown): TelegramWebAppLike | null {
  if (!isRecord(value)) {
    return null;
  }

  const ready = value.ready;
  const expand = value.expand;

  return {
    initData: readString(value.initData) ?? '',
    initDataUnsafe: isRecord(value.initDataUnsafe) ? value.initDataUnsafe : null,
    themeParams: isRecord(value.themeParams) ? value.themeParams : {},
    colorScheme: value.colorScheme === 'dark' || value.colorScheme === 'light' ? value.colorScheme : null,
    viewportHeight: readPositiveNumber(value.viewportHeight),
    viewportStableHeight: readPositiveNumber(value.viewportStableHeight),
    isExpanded: typeof value.isExpanded === 'boolean' ? value.isExpanded : null,
    ready: typeof ready === 'function' ? () => ready.call(value) : undefined,
    expand: typeof expand === 'function' ? () => expand.call(value) : undefined
  };
}

export function extractTelegramMiniAppBootstrapState(
  webAppValue: unknown,
  launchParams: URLSearchParams = new URLSearchParams()
): TelegramMiniAppBootstrapState | null {
  const webApp = normalizeTelegramWebApp(webAppValue);
  if (!webApp) {
    return null;
  }

  const startParam = readStartParam(webApp, launchParams);
  const dataAttributes: Record<string, string> = {
    'data-telegram-miniapp': 'true'
  };

  if (webApp.colorScheme) {
    dataAttributes['data-telegram-color-scheme'] = webApp.colorScheme;
  }
  if (webApp.isExpanded !== null) {
    dataAttributes['data-telegram-expanded'] = String(webApp.isExpanded);
  }

  return {
    initData: webApp.initData,
    startParam,
    startRoute: mapTelegramStartParamToRoute(startParam),
    dataAttributes,
    classNames: ['telegram-miniapp'],
    cssVariables: {
      ...extractTelegramThemeVariables(webApp.themeParams),
      ...extractTelegramViewportVariables(webApp)
    }
  };
}

export function telegramLaunchParamsFromUrl(url: Pick<Location, 'hash' | 'search'> | URL | string): URLSearchParams {
  const locationLike = typeof url === 'string' ? new URL(url, 'https://miniapp.local') : url;
  const params = new URLSearchParams(locationLike.search);
  const hash = 'hash' in locationLike ? locationLike.hash : '';
  const hashParams = parseHashParams(hash);

  for (const [key, value] of hashParams) {
    if (!params.has(key)) {
      params.set(key, value);
    }
  }

  return params;
}

export function mapTelegramStartParamToRoute(payload: unknown): string | null {
  const startParam = normalizeStartParam(payload);
  if (!startParam) {
    return null;
  }

  if (startParam.startsWith('invite_')) {
    return pathForStartParamPayload('/collection/invite', startParam.slice('invite_'.length));
  }

  if (startParam.startsWith('meme_')) {
    return pathForStartParamPayload('/memes', startParam.slice('meme_'.length));
  }

  return null;
}

export function extractTelegramThemeVariables(themeParams: unknown): Record<string, string> {
  if (!isRecord(themeParams)) {
    return {};
  }

  const variables: Record<string, string> = {};
  for (const [themeParam, cssVariable] of Object.entries(THEME_PARAM_TO_CSS_VARIABLE)) {
    const value = readString(themeParams[themeParam])?.trim();
    if (value) {
      variables[cssVariable] = value;
    }
  }
  return variables;
}

export function extractTelegramViewportVariables(webAppValue: unknown): Record<string, string> {
  const webApp = normalizeTelegramWebApp(webAppValue);
  if (!webApp) {
    return {};
  }

  return {
    ...(webApp.viewportHeight ? { '--tg-viewport-height': `${webApp.viewportHeight}px` } : {}),
    ...(webApp.viewportStableHeight ? { '--tg-viewport-stable-height': `${webApp.viewportStableHeight}px` } : {})
  };
}

function readStartParam(webApp: TelegramWebAppLike, launchParams: URLSearchParams): string | null {
  return (
    normalizeStartParam(webApp.initDataUnsafe?.start_param) ??
    normalizeStartParam(launchParams.get('tgWebAppStartParam')) ??
    normalizeStartParam(new URLSearchParams(webApp.initData).get('start_param'))
  );
}

function normalizeStartParam(value: unknown): string | null {
  const startParam = readString(value)?.trim();
  if (!startParam || startParam.length > MAX_START_PARAM_LENGTH) {
    return null;
  }

  return startParam;
}

function pathForStartParamPayload(basePath: string, payload: string): string | null {
  const normalized = payload.trim();
  if (!normalized || normalized.length > MAX_START_PARAM_LENGTH) {
    return null;
  }

  return `${basePath}/${encodeURIComponent(normalized)}`;
}

function parseHashParams(hash: string): URLSearchParams {
  const normalized = hash.startsWith('#') ? hash.slice(1) : hash;
  if (!normalized || normalized.startsWith('/')) {
    return new URLSearchParams();
  }

  return new URLSearchParams(normalized.startsWith('?') ? normalized.slice(1) : normalized);
}

function parseLaunchThemeParams(value: string | null): Record<string, unknown> {
  const normalized = value?.trim();
  if (!normalized) {
    return {};
  }

  try {
    const parsed = JSON.parse(normalized) as unknown;
    return isRecord(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function normalizeColorScheme(value: unknown): 'dark' | 'light' | null {
  return value === 'dark' || value === 'light' ? value : null;
}

function readPositiveNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null;
}

function readString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object';
}
