import { fail, type RequestEvent } from '@sveltejs/kit';
import {
  ApiError,
  importAdminSearchSynonymSeed,
  publishAdminSearchSynonymDraft,
  resetAdminSearchSynonymDraft,
  retryAdminSearchSynonymSync,
  updateAdminSearchSynonymDraft
} from '$lib/api/client';
import type {
  AdminSearchSynonymLocale,
  AdminSearchSynonymValidationIssue,
  AdminSearchSynonymValidationRead
} from '$lib/api/types';
import {
  apiRequest,
  readAuditReason,
  readRequestId,
  readRequired,
  runAction
} from './actionUtils';

type SearchSynonymActionEvent = Pick<RequestEvent, 'fetch' | 'request'>;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MAX_SOURCE_TEXT_LENGTH = 1_000_000;

export async function saveSearchSynonymDraft({ fetch, request }: SearchSynonymActionEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const locale = readLocale(data);
    await updateAdminSearchSynonymDraft(
      {
        ...apiRequest(fetch, request),
        body: {
          request_id: readRequestId(data),
          version: readRequired(data, 'version'),
          source_text: readSourceText(data),
          reason: readAuditReason(data)
        }
      },
      locale
    );
    return { message: `${localeLabel(locale)} synonym draft saved.`, locale };
  });
}

export async function importSearchSynonymSeed({ fetch, request }: SearchSynonymActionEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const locale = readLocale(data);
    await importAdminSearchSynonymSeed(
      {
        ...apiRequest(fetch, request),
        body: mutationBody(data)
      },
      locale
    );
    return { message: `${localeLabel(locale)} research seed loaded into the draft.`, locale };
  });
}

export async function publishSearchSynonymDraft({ fetch, request }: SearchSynonymActionEvent) {
  const data = await request.formData();
  let locale: AdminSearchSynonymLocale | null = null;
  try {
    locale = readLocale(data);
    await publishAdminSearchSynonymDraft(
      {
        ...apiRequest(fetch, request),
        body: {
          ...mutationBody(data),
          confirm_destructive: readDestructiveConfirmation(data)
        }
      },
      locale
    );
    return { message: `${localeLabel(locale)} synonym revision published.`, locale };
  } catch (caught) {
    if (caught instanceof ApiError) {
      const publishValidation = readPublishValidation(caught.detail);
      return fail(caught.status, {
        message: caught.message,
        error: true,
        ...(locale && publishValidation ? { locale, publishValidation } : {})
      });
    }
    if (caught instanceof Error) {
      return fail(400, { message: caught.message, error: true });
    }
    return fail(500, { message: 'Admin operation failed.', error: true });
  }
}

export async function resetSearchSynonymDraft({ fetch, request }: SearchSynonymActionEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const locale = readLocale(data);
    const revisionId = readRevisionId(data);
    await resetAdminSearchSynonymDraft(
      {
        ...apiRequest(fetch, request),
        body: {
          ...mutationBody(data),
          revision_id: revisionId
        }
      },
      locale
    );
    return {
      message: revisionId
        ? `${localeLabel(locale)} revision restored into the draft.`
        : `${localeLabel(locale)} draft reset to the published revision.`,
      locale
    };
  });
}

export async function retrySearchSynonymSync({ fetch, request }: SearchSynonymActionEvent) {
  const data = await request.formData();
  return runAction(async () => {
    await retryAdminSearchSynonymSync({
      ...apiRequest(fetch, request),
      body: {
        request_id: readRequestId(data),
        version: readRequired(data, 'version'),
        reason: readAuditReason(data)
      }
    });
    return { message: 'Meilisearch synonym reconciliation requested.' };
  });
}

function mutationBody(data: FormData) {
  return {
    request_id: readRequestId(data),
    version: readRequired(data, 'version'),
    reason: readAuditReason(data)
  };
}

function readSourceText(data: FormData): string {
  const value = data.get('source_text');
  if (value !== null && typeof value !== 'string') {
    throw new Error('source_text must be text.');
  }
  const sourceText = (value ?? '').replace(/\r\n?/g, '\n');
  if (sourceText.length > MAX_SOURCE_TEXT_LENGTH) {
    throw new Error(`source_text must be at most ${MAX_SOURCE_TEXT_LENGTH.toLocaleString('en-US')} characters.`);
  }
  return sourceText;
}

function readDestructiveConfirmation(data: FormData): boolean {
  const value = data.get('confirm_destructive');
  if (value === null) return false;
  if (value === 'true') return true;
  throw new Error('confirm_destructive must be true when provided.');
}

function readRevisionId(data: FormData): string | null {
  const revisionId = String(data.get('revision_id') ?? '').trim();
  if (!revisionId) return null;
  if (!UUID_PATTERN.test(revisionId)) {
    throw new Error('revision_id must be a UUID.');
  }
  return revisionId;
}

function readLocale(data: FormData): AdminSearchSynonymLocale {
  const locale = readRequired(data, 'locale');
  if (locale !== 'en' && locale !== 'ru') {
    throw new Error('locale must be en or ru.');
  }
  return locale;
}

function localeLabel(locale: AdminSearchSynonymLocale): string {
  return locale === 'en' ? 'English' : 'Russian';
}

function readPublishValidation(detail: unknown): AdminSearchSynonymValidationRead | null {
  if (!isRecord(detail) || !isSearchSynonymValidation(detail.validation)) return null;
  return detail.validation;
}

function isSearchSynonymValidation(value: unknown): value is AdminSearchSynonymValidationRead {
  return isRecord(value)
    && typeof value.valid === 'boolean'
    && isNonNegativeInteger(value.group_count)
    && isNonNegativeInteger(value.compiled_key_count)
    && isNonNegativeInteger(value.edge_count)
    && isNonNegativeInteger(value.payload_bytes)
    && Array.isArray(value.issues)
    && value.issues.every(isSearchSynonymValidationIssue);
}

function isSearchSynonymValidationIssue(value: unknown): value is AdminSearchSynonymValidationIssue {
  return isRecord(value)
    && (value.level === 'error' || value.level === 'warning')
    && typeof value.code === 'string'
    && typeof value.message === 'string'
    && (value.line_number === null || isNonNegativeInteger(value.line_number))
    && (value.term === null || typeof value.term === 'string');
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export const searchSynonymActions = {
  saveSearchSynonymDraft,
  importSearchSynonymSeed,
  publishSearchSynonymDraft,
  resetSearchSynonymDraft,
  retrySearchSynonymSync
};
