import type { AdminTelegramSessionRead, TelegramSessionStatus } from '$lib/api/types';

export type TelegramAccountStatus = 'Ready' | 'Sign-in needed' | 'Temporarily rate-limited' | 'Needs attention' | 'Disabled' | 'Stopped';
export type TelegramAccountAction = 'Connect' | 'Enable' | 'Resume' | 'Validate' | null;

export interface TelegramPasswordLoginStep {
  kind: 'password';
  method: 'phone' | 'qr';
  sessionId: string | null;
  attemptId: string;
  phoneHint?: string | null;
  message: string;
  error?: boolean;
}

export interface TelegramPhoneCodeLoginStep {
  kind: 'phone_code';
  sessionId: string | null;
  attemptId: string;
  phoneHint?: string | null;
  expiresAt?: string;
  message: string;
  error?: boolean;
}

export interface TelegramQrLoginStep {
  kind: 'qr';
  sessionId: string | null;
  attemptId: string;
  qrUrl: string;
  expiresAt?: string;
  message: string;
  error?: boolean;
  refreshing?: boolean;
}

export type TelegramLoginStep = TelegramPhoneCodeLoginStep | TelegramPasswordLoginStep;

export interface TelegramLoginError {
  kind: 'login_error';
  method: 'phone' | 'qr';
  sessionId: string | null;
  attemptId?: string;
  phoneHint?: string | null;
  message: string;
  error: true;
}

export type TelegramLoginState = TelegramLoginStep | TelegramLoginError;

export interface TelegramAccountViewModel {
  id: string;
  displayName: string;
  identity: string;
  status: TelegramAccountStatus;
  statusDetail: string;
  primaryAction: TelegramAccountAction;
  sourceCountLabel: string;
  heartbeatLabel: string;
  errorSummary: string | null;
  providerDetailsHidden: boolean;
  canReconnect: boolean;
  technicalName: string;
  internalStatus: TelegramSessionStatus;
  hasStoredCredential: boolean;
  lastHeartbeatAt: string;
  liveListenerStartedAt: string;
  floodWaitUntil: string;
  quarantinedAt: string;
  createdAt: string;
  updatedAt: string;
  errorClass: string | null;
}

export function toTelegramAccountViewModel(account: AdminTelegramSessionRead, now = new Date()): TelegramAccountViewModel {
  const status = telegramAccountStatus(account, now);
  const errorClass = safeDiagnosticText(account.last_error_class);

  return {
    id: account.id,
    displayName: safeRoutineText(account.display_name, 'Telegram account'),
    identity: accountIdentity(account),
    status,
    statusDetail: accountStatusDetail(status),
    primaryAction: accountPrimaryAction(status),
    sourceCountLabel: sourceCountLabel(account.owned_channel_count),
    heartbeatLabel: relativeTimestamp(account.last_heartbeat_at, now),
    errorSummary: errorClass,
    providerDetailsHidden: account.last_error_text !== null,
    canReconnect: status === 'Needs attention',
    technicalName: safeTechnicalName(account.name),
    internalStatus: account.status,
    hasStoredCredential: account.has_string_session,
    lastHeartbeatAt: technicalTimestamp(account.last_heartbeat_at),
    liveListenerStartedAt: technicalTimestamp(account.live_listener_started_at),
    floodWaitUntil: technicalTimestamp(account.flood_wait_until),
    quarantinedAt: technicalTimestamp(account.quarantined_at),
    createdAt: technicalTimestamp(account.created_at),
    updatedAt: technicalTimestamp(account.updated_at),
    errorClass
  };
}

export function telegramAccountStatus(account: AdminTelegramSessionRead, now = new Date()): TelegramAccountStatus {
  if (hasCurrentFloodWait(account.flood_wait_until, now)) return 'Temporarily rate-limited';
  if (!account.enabled) return 'Disabled';
  if (account.status === 'stopped') return 'Stopped';
  if (account.status === 'quarantined' || account.status === 'flood_wait' || account.quarantined_at !== null) return 'Needs attention';
  if (account.status === 'auth_required' || !account.has_string_session) return 'Sign-in needed';
  return 'Ready';
}

export function accountIdentity(account: Pick<AdminTelegramSessionRead, 'account_username' | 'account_phone_hint'>): string {
  const username = safeUsername(account.account_username);
  if (username) return `@${username}`;
  const phoneHint = safePhoneHint(account.account_phone_hint);
  return phoneHint ?? 'Telegram identity unavailable';
}

export function safePhoneHint(value: string | null | undefined): string | null {
  if (!value) return null;
  const match = value.match(/(?:ending|last)\D*(\d{2,4})\s*$/i);
  return match ? `Phone ending ${match[1]}` : null;
}

export function safeOperatorMessage(value: string, fallback = 'Sensitive account detail was redacted.'): string {
  return containsSensitiveText(value) ? fallback : truncate(value, 240);
}

export function qrLoginStep(value: unknown): TelegramQrLoginStep | null {
  const candidate = record(value);
  if (
    candidate?.kind === 'qr' &&
    (candidate.sessionId === null || typeof candidate.sessionId === 'string') &&
    typeof candidate.attemptId === 'string' &&
    typeof candidate.qrUrl === 'string'
  ) {
    return {
      kind: 'qr',
      sessionId: candidate.sessionId,
      attemptId: candidate.attemptId,
      qrUrl: candidate.qrUrl,
      expiresAt: typeof candidate.expiresAt === 'string' ? candidate.expiresAt : undefined,
      message: typeof candidate.message === 'string' ? candidate.message : 'Waiting for scan…',
      error: candidate.error === true,
      refreshing: candidate.refreshing === true
    };
  }
  return null;
}

export function passwordLoginStep(value: unknown): TelegramPasswordLoginStep | null {
  const candidate = record(value);
  if (
    candidate?.kind === 'password' &&
    (candidate.method === 'phone' || candidate.method === 'qr') &&
    (candidate.sessionId === null || typeof candidate.sessionId === 'string') &&
    typeof candidate.attemptId === 'string'
  ) {
    return {
      kind: 'password',
      method: candidate.method,
      sessionId: candidate.sessionId,
      attemptId: candidate.attemptId,
      phoneHint: typeof candidate.phoneHint === 'string' ? candidate.phoneHint : null,
      message: safeOperatorMessage(typeof candidate.message === 'string' ? candidate.message : 'Telegram requires the account password to finish.'),
      error: candidate.error === true
    };
  }
  return null;
}

export function loginError(value: unknown): TelegramLoginError | null {
  const candidate = record(value);
  if (
    candidate?.kind !== 'login_error' ||
    (candidate.method !== 'phone' && candidate.method !== 'qr') ||
    (candidate.sessionId !== null && typeof candidate.sessionId !== 'string')
  ) {
    return null;
  }
  return {
    kind: 'login_error',
    method: candidate.method,
    sessionId: candidate.sessionId,
    attemptId: typeof candidate.attemptId === 'string' ? candidate.attemptId : undefined,
    phoneHint: typeof candidate.phoneHint === 'string' ? candidate.phoneHint : null,
    message: safeOperatorMessage(typeof candidate.message === 'string' ? candidate.message : 'Telegram sign-in could not continue.'),
    error: true
  };
}

export function loginStateForAccount(
  form: unknown,
  accountId: string,
  qrPasswordStep: TelegramPasswordLoginStep | null
): TelegramLoginState | null {
  if (qrPasswordStep?.sessionId === accountId) return qrPasswordStep;

  const failedLogin = loginError(form);
  if (failedLogin?.sessionId === accountId) return failedLogin;

  const candidate = record(form);
  if (!candidate || candidate.sessionId !== accountId || typeof candidate.attemptId !== 'string') return null;

  if (candidate.kind === 'phone_code') {
    return {
      kind: 'phone_code',
      sessionId: accountId,
      attemptId: candidate.attemptId,
      phoneHint: typeof candidate.phoneHint === 'string' ? candidate.phoneHint : null,
      expiresAt: typeof candidate.expiresAt === 'string' ? candidate.expiresAt : undefined,
      message: safeOperatorMessage(typeof candidate.message === 'string' ? candidate.message : 'Enter the code from Telegram.'),
      error: candidate.error === true
    };
  }

  return passwordLoginStep(candidate);
}

export function loginStateForNewAccount(
  form: unknown,
  qrPasswordStep: TelegramPasswordLoginStep | null
): TelegramLoginState | null {
  if (qrPasswordStep?.sessionId === null) return qrPasswordStep;

  const failedLogin = loginError(form);
  if (failedLogin?.sessionId === null) return failedLogin;

  const candidate = record(form);
  if (!candidate || candidate.sessionId !== null || typeof candidate.attemptId !== 'string') return null;

  if (candidate.kind === 'phone_code') {
    return {
      kind: 'phone_code',
      sessionId: null,
      attemptId: candidate.attemptId,
      phoneHint: typeof candidate.phoneHint === 'string' ? candidate.phoneHint : null,
      expiresAt: typeof candidate.expiresAt === 'string' ? candidate.expiresAt : undefined,
      message: safeOperatorMessage(typeof candidate.message === 'string' ? candidate.message : 'Enter the code from Telegram.'),
      error: candidate.error === true
    };
  }

  return passwordLoginStep(candidate);
}

function accountPrimaryAction(status: TelegramAccountStatus): TelegramAccountAction {
  if (status === 'Ready' || status === 'Needs attention') return 'Validate';
  if (status === 'Sign-in needed') return 'Connect';
  if (status === 'Disabled') return 'Enable';
  if (status === 'Stopped') return 'Resume';
  return null;
}

function accountStatusDetail(status: TelegramAccountStatus): string {
  if (status === 'Ready') return 'Connected and ready to fetch.';
  if (status === 'Sign-in needed') return 'Sign in before this account can fetch sources.';
  if (status === 'Temporarily rate-limited') return 'Wait for Telegram’s rate limit to end before taking account actions.';
  if (status === 'Disabled') return 'This account is disabled by policy.';
  if (status === 'Stopped') return 'This account has been stopped.';
  return 'Review the account diagnostics and repair settings.';
}

function sourceCountLabel(count: number): string {
  return `${count} ${count === 1 ? 'source' : 'sources'}`;
}

function relativeTimestamp(value: string | null, now: Date): string {
  if (!value) return 'No heartbeat yet';
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return 'Heartbeat time unavailable';
  const seconds = Math.max(0, Math.floor((now.getTime() - timestamp) / 1_000));
  if (seconds >= 86_400) return `${Math.floor(seconds / 86_400)}d ago`;
  if (seconds >= 3_600) return `${Math.floor(seconds / 3_600)}h ago`;
  if (seconds >= 60) return `${Math.floor(seconds / 60)}m ago`;
  return `${seconds}s ago`;
}

function safeUsername(value: string | null): string | null {
  if (!value || !/^[A-Za-z0-9_]{3,32}$/.test(value) || containsSensitiveText(value)) return null;
  return value;
}

function safeTechnicalName(value: string): string {
  return containsSensitiveText(value) ? 'Redacted technical name' : truncate(value, 120);
}

function safeRoutineText(value: string | null | undefined, fallback: string): string {
  if (!value || containsSensitiveText(value)) return fallback;
  return truncate(value, 120);
}

function safeDiagnosticText(value: string | null): string | null {
  if (!value) return null;
  return safeOperatorMessage(value);
}

function technicalTimestamp(value: string | null): string {
  if (!value) return 'Not recorded';
  return Number.isFinite(new Date(value).getTime()) && /^\d{4}-\d{2}-\d{2}T/.test(value) ? value : 'Invalid timestamp';
}

function containsSensitiveText(value: string): boolean {
  return /string[\s_-]*session|encrypted|secret|password\s*[:=]|authorization|api[\s_-]*key|bearer\s|gAAAA[A-Za-z0-9_-]{16,}|\b[A-Za-z0-9+/_-]{40,}={0,2}\b|\+?\d[\d\s().-]{6,}\d/i.test(value);
}

function truncate(value: string, limit: number): string {
  const normalized = value.replace(/\s+/g, ' ').trim();
  return normalized.length > limit ? `${normalized.slice(0, limit - 1)}…` : normalized;
}

function hasCurrentFloodWait(value: string | null, now: Date): boolean {
  if (!value) return false;
  const timestamp = new Date(value).getTime();
  return !Number.isFinite(timestamp) || timestamp > now.getTime();
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' ? (value as Record<string, unknown>) : null;
}
