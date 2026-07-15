import type { RequestEvent } from '@sveltejs/kit';
import type { AdminRecoveryCapability, AdminRecoveryWorkKind, AdminRecoveryWorkReference } from '$lib/api/types';
import {
  ApiError,
  cancelAdminRecoveryBatch,
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
  'rebuild_outbox',
  'recover_dead_letter',
  'reinspect_ingest',
  'replay_source_post',
  'resync_target',
  'resume_backfill',
  'retry_stage'
]);

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
    const batch = await previewAdminRecoveryBatch({
      ...apiRequest(fetch, request),
      body: {
        request_id: readRequestId(data),
        reason: readAuditReason(data),
        capability,
        items: recoveryWorkReferences(data)
      }
    });
    return {
      message: `Preview created for ${batch.total_count.toLocaleString('en-US')} item${batch.total_count === 1 ? '' : 's'}. Review it before scheduling.`,
      batch
    };
  });
}

export async function scheduleRecoveryBatch({ fetch, request }: RecoveryActionEvent) {
  const data = await request.formData();
  return runAction(async () => {
    requireConfirmation(
      readRequired(data, 'confirmation_phrase'),
      'SCHEDULE',
      'Type SCHEDULE to dispatch this recovery batch.'
    );
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
    requireConfirmation(
      readRequired(data, 'confirmation_phrase'),
      'CANCEL',
      'Type CANCEL to cancel undispatched recovery items.'
    );
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
    return { message: 'Undispatched recovery items cancelled.', batch };
  });
}

function readRecoveryCapability(data: FormData): AdminRecoveryCapability {
  const capability = readRequired(data, 'capability') as AdminRecoveryCapability;
  if (!RECOVERY_CAPABILITIES.has(capability)) {
    throw new ApiError(400, 'Unknown recovery capability.');
  }
  return capability;
}

function readRecoveryWorkKind(data: FormData): AdminRecoveryWorkKind {
  const kind = readRequired(data, 'kind') as AdminRecoveryWorkKind;
  if (!RECOVERY_WORK_KINDS.has(kind)) {
    throw new ApiError(400, 'Unknown recovery work kind.');
  }
  return kind;
}

function recoveryWorkReferences(data: FormData): AdminRecoveryWorkReference[] {
  const references = data.getAll('item').map((value) => parseWorkReference(String(value)));
  if (!references.length) {
    throw new ApiError(400, 'Select at least one recovery item to preview.');
  }
  if (references.length > 1000) {
    throw new ApiError(400, 'A recovery preview can contain at most 1000 items.');
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

function capabilityMessageSubject(capability: AdminRecoveryCapability): string {
  const subjects: Record<AdminRecoveryCapability, string> = {
    archive_dead_letter: 'Dead-letter archival',
    rebuild_outbox: 'Outbox rebuild',
    recover_dead_letter: 'Dead-letter recovery',
    reinspect_ingest: 'Media re-inspection',
    replay_source_post: 'Post replay',
    resync_target: 'Target resync',
    resume_backfill: 'Backfill resume',
    retry_stage: 'Stage retry'
  };
  return subjects[capability];
}

export const recoveryActions = {
  retryRecoveryWork,
  previewRecoveryBatch,
  scheduleRecoveryBatch,
  cancelRecoveryBatch
};
