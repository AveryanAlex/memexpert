import type { CurrentSessionRead, TelegramLinkStartRead } from '$lib/api/types';

export const TELEGRAM_START_PREFIX = 'link_';
export const TELEGRAM_LOGIN_POLL_INTERVAL_MS = 1000;

export interface LoginProviderOption {
  id: 'telegram' | 'google' | 'email';
  label: string;
  description: string;
  status: 'available' | 'coming_later';
}

export interface SingleFlightPollLoop<T> {
  start: () => void;
  stop: () => void;
  isRunning: () => boolean;
}

export interface SingleFlightPollLoopOptions<T> {
  intervalMs: number;
  request: (signal: AbortSignal) => Promise<T>;
  onResult: (result: T) => boolean | void | Promise<boolean | void>;
  onError?: (error: unknown) => void;
}

export interface TelegramSessionRefreshResult {
  completed: boolean;
  shouldContinuePolling: boolean;
  errorMessage: string | null;
}

export const TELEGRAM_SESSION_REFRESH_ERROR = 'Telegram connected, but MemeXpert could not refresh your session. Retrying automatically…';

export const LOGIN_PROVIDER_OPTIONS: LoginProviderOption[] = [
  { id: 'telegram', label: 'Telegram', description: 'Fast bot handoff, no password.', status: 'available' },
  { id: 'google', label: 'Google', description: 'OAuth sign-in will be available later.', status: 'coming_later' },
  { id: 'email', label: 'Email', description: 'Magic links and password login will be available later.', status: 'coming_later' }
];

export function buildTelegramStartCommand(link: Pick<TelegramLinkStartRead, 'code'>): string {
  return `/start ${TELEGRAM_START_PREFIX}${link.code}`;
}

export function telegramExpiryLabel(link: Pick<TelegramLinkStartRead, 'expires_in_seconds'>): string {
  if (link.expires_in_seconds < 60) return 'Expires in less than 1 minute';
  return `Expires in about ${Math.ceil(link.expires_in_seconds / 60)} minutes`;
}

export function isFullSession(session: CurrentSessionRead | null): boolean {
  return session?.user.account_type === 'full';
}

export async function refreshTelegramSession(invalidateSession: () => Promise<void>): Promise<TelegramSessionRefreshResult> {
  try {
    await invalidateSession();
    return { completed: true, shouldContinuePolling: false, errorMessage: null };
  } catch {
    return {
      completed: false,
      shouldContinuePolling: true,
      errorMessage: TELEGRAM_SESSION_REFRESH_ERROR
    };
  }
}

export function createSingleFlightPollLoop<T>({
  intervalMs,
  request,
  onResult,
  onError
}: SingleFlightPollLoopOptions<T>): SingleFlightPollLoop<T> {
  let sequence = 0;
  let running = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let activeController: AbortController | null = null;

  function isCurrent(requestSequence: number): boolean {
    return running && requestSequence === sequence;
  }

  async function poll(requestSequence: number): Promise<void> {
    if (!isCurrent(requestSequence)) return;

    const controller = new AbortController();
    activeController = controller;
    let shouldContinue: boolean | void = true;

    try {
      const result = await request(controller.signal);
      if (!isCurrent(requestSequence)) return;
      shouldContinue = await onResult(result);
    } catch (error) {
      if (!isCurrent(requestSequence) || controller.signal.aborted) return;
      onError?.(error);
    } finally {
      if (activeController === controller) activeController = null;
    }

    if (!isCurrent(requestSequence)) return;
    if (shouldContinue === false) {
      running = false;
      return;
    }

    timer = setTimeout(() => {
      timer = null;
      void poll(requestSequence);
    }, intervalMs);
  }

  function stop(): void {
    sequence += 1;
    running = false;
    if (timer !== null) clearTimeout(timer);
    timer = null;
    activeController?.abort();
    activeController = null;
  }

  function start(): void {
    stop();
    running = true;
    void poll(sequence);
  }

  return {
    start,
    stop,
    isRunning: () => running
  };
}
