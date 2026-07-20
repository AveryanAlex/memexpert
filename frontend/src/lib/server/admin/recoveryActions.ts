import type { RequestEvent } from '@sveltejs/kit';
import type {
  AdminRecoveryBatchSelector,
  AdminRecoveryCapability,
  AdminRecoveryReplayScope,
  AdminRecoveryRetryLimit,
  AdminRecoveryWorkKind,
  AdminRecoveryWorkReference
} from '$lib/api/types';
import {
  ApiError,
  actOnAdminRecoveryWork,
  cancelAdminRecoveryBatch,
  handoffAdminRecoveryBatch,
  previewFailedAdminRecoveryBatch,
  previewAdminRecoveryBatch,
  retryAdminRecoveryWork,
  scheduleAdminRecoveryBatch
} from '$lib/api/client';
import {
  apiRequest,
  readAuditReason,
  readRequestId,
  readRequired,
  requireConfirmation,
  runAction
} from './actionUtils';

const RECOVERY_CAPABILITIES = new Set<AdminRecoveryCapability>([
  'archive_dead_letter',
  'regenerate_derivatives',
  'rebuild_outbox',
  'recover_dead_letter',
  'reinspect_ingest',
  'replay_stage',
  'replay_source_post',
  'resync_target',
  'resume_backfill',
  'retry_stage'
]);

const RECOVERY_SCOPES = new Set<AdminRecoveryReplayScope>(['stage_only', 'stage_and_dependents']);
const RECOVERY_RETRY_LIMITS = new Set<AdminRecoveryRetryLimit>([1, 3, 5]);

const RECOVERY_WORK_KINDS = new Set<AdminRecoveryWorkKind>([
  'backfill',
  'dead_letter',
  'ingest_request',
  'outbox',
  'pipeline_stage',
  'source_post',
  'sync_target'
]);

type RecoveryActionEvent = Pick<RequestEvent, 'fetch' | 'request'>;

export async function actRecoveryWork({ fetch, request }: RecoveryActionEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const action = readRecoveryCapability(data);
    requireArchiveConfirmation(data, action);
    const result = await actOnAdminRecoveryWork(
      {
        ...apiRequest(fetch, request),
        body: {
          request_id: readRequestId(data),
          version: readRequired(data, 'version'),
          reason: readAuditReason(data),
          action,
          scope: readRecoveryScope(data),
          retry_limit: readRecoveryRetryLimit(data),
          acknowledgements: readAcknowledgements(data)
        }
      },
      readRecoveryWorkKind(data),
      readRequired(data, 'work_id')
    );
    return {
      message: `${capabilityMessageSubject(action)} queued.`,
      recoveryJobId: result.id,
      batch: result
    };
  });
}

export async function retryRecoveryWork({ fetch, request }: RecoveryActionEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const capability = readRecoveryCapability(data);
    requireArchiveConfirmation(data, capability);
    const result = await retryAdminRecoveryWork(
      {
        ...apiRequest(fetch, request),
        body: {
          request_id: readRequestId(data),
          version: readRequired(data, 'version'),
          reason: readAuditReason(data),
          capability
        }
      },
      readRecoveryWorkKind(data),
      readRequired(data, 'work_id')
    );
    return {
      message: `${capabilityMessageSubject(capability)} queued.`,
      recoveryJobId: result.id
    };
  });
}

export async function previewRecoveryBatch({ fetch, request }: RecoveryActionEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const capability = readRecoveryCapability(data);
    requireArchiveConfirmation(data, capability);
    const selector = recoveryBatchSelector(data);
    const batch = await previewAdminRecoveryBatch({
      ...apiRequest(fetch, request),
      body: {
        request_id: readRequestId(data),
        reason: readAuditReason(data),
        action: capability,
        scope: readRecoveryScope(data),
        retry_limit: readRecoveryRetryLimit(data),
        selector,
        acknowledgements: readAcknowledgements(data)
      }
    });
    const preparing = batch.status === 'preparing';
    return {
      message: preparing
        ? 'Exact preview preparation started. You can leave this page while the scheduler materializes every match.'
        : `Preview created for ${batch.total_count.toLocaleString('en-US')} execution step${batch.total_count === 1 ? '' : 's'}. Review it before scheduling.`,
      batch
    };
  });
}

export async function scheduleRecoveryBatch({ fetch, request }: RecoveryActionEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const batch = await scheduleAdminRecoveryBatch(
      {
        ...apiRequest(fetch, request),
        body: {
          version: readRequired(data, 'version'),
          reason: readAuditReason(data)
        }
      },
      readRequired(data, 'job_id')
    );
    return { message: 'Recovery batch scheduled.', batch };
  });
}

export async function cancelRecoveryBatch({ fetch, request }: RecoveryActionEvent) {
  const data = await request.formData();
  return runAction(async () => {
    requireAcknowledgement(data, 'acknowledge_cancel', 'Acknowledge that dispatched work will reconcile before cancellation finishes.');
    const batch = await cancelAdminRecoveryBatch(
      {
        ...apiRequest(fetch, request),
        body: {
          version: readRequired(data, 'version'),
          reason: readAuditReason(data)
        }
      },
      readRequired(data, 'job_id')
    );
    return {
      message: batch.status === 'cancelled'
        ? 'Recovery job discarded.'
        : 'Cancellation started. Dispatched work will reconcile before totals finalize.',
      batch
    };
  });
}

export async function retryFailedRecoveryBatch({ fetch, request }: RecoveryActionEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const batch = await previewFailedAdminRecoveryBatch(
      {
        ...apiRequest(fetch, request),
        body: {
          request_id: readRequestId(data),
          version: readRequired(data, 'version'),
          reason: readAuditReason(data),
          retry_limit: readRecoveryRetryLimit(data)
        }
      },
      readRequired(data, 'job_id')
    );
    return {
      message: `Retry preview created for ${batch.selected_root_count ?? batch.total_count} failed root${(batch.selected_root_count ?? batch.total_count) === 1 ? '' : 's'}.`,
      recoveryJobId: batch.id,
      batch
    };
  });
}

export async function handoffRecoveryBatch({ fetch, request }: RecoveryActionEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const batch = await handoffAdminRecoveryBatch(
      {
        ...apiRequest(fetch, request),
        body: {
          version: readRequired(data, 'version'),
          reason: readAuditReason(data),
          assigned_admin_user_id: readRequired(data, 'assigned_admin_user_id')
        }
      },
      readRequired(data, 'job_id')
    );
    return { message: 'Operational handoff recorded. The original requester remains in the audit history.', batch };
  });
}

function readRecoveryCapability(data: FormData): AdminRecoveryCapability {
  const capability = String(data.get('action') ?? data.get('capability') ?? '').trim() as AdminRecoveryCapability;
  if (!capability) throw new ApiError(400, 'action is required.');
  if (!RECOVERY_CAPABILITIES.has(capability)) {
    throw new ApiError(400, 'Unknown recovery capability.');
  }
  return capability;
}

function readRecoveryScope(data: FormData): AdminRecoveryReplayScope {
  const scope = String(data.get('scope') ?? 'stage_only').trim() as AdminRecoveryReplayScope;
  if (!RECOVERY_SCOPES.has(scope)) throw new ApiError(400, 'Unknown replay scope.');
  return scope;
}

function readRecoveryRetryLimit(data: FormData): AdminRecoveryRetryLimit {
  const value = Number(String(data.get('retry_limit') ?? '3').trim()) as AdminRecoveryRetryLimit;
  if (!RECOVERY_RETRY_LIMITS.has(value)) throw new ApiError(400, 'Retry limit must be 1, 3, or 5.');
  return value;
}

function readAcknowledgements(data: FormData): string[] {
  return [...new Set(data.getAll('acknowledgement').map((value) => String(value).trim()).filter(Boolean))];
}

function readRecoveryWorkKind(data: FormData): AdminRecoveryWorkKind {
  const kind = readRequired(data, 'kind') as AdminRecoveryWorkKind;
  if (!RECOVERY_WORK_KINDS.has(kind)) {
    throw new ApiError(400, 'Unknown recovery work kind.');
  }
  return kind;
}

function recoveryBatchSelector(data: FormData): AdminRecoveryBatchSelector {
  const selectorType = String(data.get('selector_type') ?? 'explicit').trim();
  if (selectorType === 'explicit') return { type: 'explicit', items: recoveryWorkReferences(data) };
  if (selectorType !== 'query') throw new ApiError(400, 'Unknown recovery selector.');
  const snapshotAt = readRequired(data, 'snapshot_at');
  const rawFilters = readRequired(data, 'query_filters');
  let filters: unknown;
  try {
    filters = JSON.parse(rawFilters);
  } catch {
    throw new ApiError(400, 'Recovery query filters are malformed.');
  }
  if (!filters || typeof filters !== 'object' || Array.isArray(filters)) {
    throw new ApiError(400, 'Recovery query filters are malformed.');
  }
  return {
    type: 'query',
    filters: filters as Record<string, string | number | boolean | string[] | null>,
    snapshot_at: snapshotAt
  };
}

function recoveryWorkReferences(data: FormData): AdminRecoveryWorkReference[] {
  const references = data.getAll('item').map((value) => parseWorkReference(String(value)));
  if (!references.length) {
    throw new ApiError(400, 'Select at least one recovery item to preview.');
  }
  return references;
}

function parseWorkReference(value: string): AdminRecoveryWorkReference {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new ApiError(400, 'Recovery item selection is malformed.');
  }
  if (!parsed || typeof parsed !== 'object') {
    throw new ApiError(400, 'Recovery item selection is malformed.');
  }
  const reference = parsed as Record<string, unknown>;
  const kind = String(reference.kind ?? '') as AdminRecoveryWorkKind;
  const id = String(reference.id ?? '').trim();
  const version = String(reference.version ?? '').trim();
  if (!RECOVERY_WORK_KINDS.has(kind) || !id || !version) {
    throw new ApiError(400, 'Recovery item selection is malformed.');
  }
  return { kind, id, version };
}

function requireArchiveConfirmation(data: FormData, capability: AdminRecoveryCapability): void {
  if (capability !== 'archive_dead_letter') return;
  requireConfirmation(
    readRequired(data, 'confirmation_phrase'),
    'ARCHIVE',
    'Type ARCHIVE to archive this dead letter.'
  );
}

function requireAcknowledgement(data: FormData, name: string, message: string): void {
  if (data.get(name) !== 'on') throw new ApiError(400, message);
}

function capabilityMessageSubject(capability: AdminRecoveryCapability): string {
  const subjects: Record<AdminRecoveryCapability, string> = {
    archive_dead_letter: 'Dead-letter archival',
    regenerate_derivatives: 'Derivative regeneration',
    rebuild_outbox: 'Outbox rebuild',
    recover_dead_letter: 'Dead-letter recovery',
    reinspect_ingest: 'Media re-inspection',
    replay_stage: 'Stage replay',
    replay_source_post: 'Post replay',
    resync_target: 'Target resync',
    resume_backfill: 'Backfill resume',
    retry_stage: 'Stage retry'
  };
  return subjects[capability];
}

export const recoveryActions = {
  actRecoveryWork,
  retryRecoveryWork,
  previewRecoveryBatch,
  scheduleRecoveryBatch,
  cancelRecoveryBatch,
  retryFailedRecoveryBatch,
  handoffRecoveryBatch
};
