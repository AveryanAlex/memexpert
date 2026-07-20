import type {
  AdminRecoveryAcknowledgementRead,
  AdminRecoveryActionCandidateRead,
  AdminRecoveryBucket,
  AdminRecoveryCapability,
  AdminRecoveryReplayScope,
  AdminRecoveryRetryLimit,
  AdminRecoveryWorkKind,
  AdminRecoveryWorkRead
} from '$lib/api/types';

export const RECOVERY_PAGE_SIZE = 50;
export const RECOVERY_JOB_PAGE_SIZE = 25;
export const RECOVERY_JOB_ITEM_PAGE_SIZE = 50;
export const RECOVERY_RETRY_LIMITS: AdminRecoveryRetryLimit[] = [1, 3, 5];

export type RecoverySection = 'jobs' | 'needs_attention' | 'regenerate';

export const RECOVERY_SECTIONS: Array<{ value: RecoverySection; label: string; description: string }> = [
  { value: 'needs_attention', label: 'Needs attention', description: 'Retryable, stuck, blocked, and dead-lettered work.' },
  { value: 'regenerate', label: 'Regenerate', description: 'Deliberate replay and derivative maintenance.' },
  { value: 'jobs', label: 'Jobs', description: 'Preview preparation, active jobs, history, and failures.' }
];

export const RECOVERY_BUCKETS: Array<{ value: AdminRecoveryBucket; label: string }> = [
  { value: 'retryable', label: 'Retryable' },
  { value: 'blocked', label: 'Blocked' },
  { value: 'stuck', label: 'Stuck' },
  { value: 'dead_lettered', label: 'Dead-lettered' }
];

export const RECOVERY_WORK_KINDS: Array<{ value: AdminRecoveryWorkKind; label: string }> = [
  { value: 'backfill', label: 'Crawler backfill' },
  { value: 'source_post', label: 'Telegram post' },
  { value: 'ingest_request', label: 'Ingest request' },
  { value: 'pipeline_stage', label: 'Pipeline stage' },
  { value: 'sync_target', label: 'Search target' },
  { value: 'outbox', label: 'Outbox event' },
  { value: 'dead_letter', label: 'Dead letter' }
];

export const RECOVERY_STAGES = [
  { value: 'ingest', label: 'Ingest' },
  { value: 'transcode', label: 'Transcode' },
  { value: 'ocr', label: 'OCR' },
  { value: 'embed', label: 'Embed' },
  { value: 'classify', label: 'Classify' },
  { value: 'sync_qdrant', label: 'Qdrant sync' },
  { value: 'sync_meili', label: 'Meilisearch sync' }
] as const;

export interface RecoveryFilters {
  section: RecoverySection;
  bucket: AdminRecoveryBucket | null;
  kind: AdminRecoveryWorkKind | null;
  source: string | null;
  stage: string | null;
  reason: string | null;
  query: string | null;
  cursor: string | null;
  jobCursor: string | null;
}

export interface RecoveryWorkspaceRequestIds {
  batchPreview: string;
  allMatchingPreview: string;
  outdatedVideoPreview: string;
  successfulStagePreview: string;
  work: Record<string, string>;
}

export function recoveryWorkRequestKey(work: Pick<AdminRecoveryWorkRead, 'kind' | 'id'>): string {
  return `${work.kind}:${work.id}`;
}

export function recoveryFiltersFromUrl(url: URL): RecoveryFilters {
  return {
    section: enumParam(url.searchParams.get('view'), RECOVERY_SECTIONS.map((option) => option.value)) ?? 'needs_attention',
    bucket: enumParam(url.searchParams.get('bucket'), RECOVERY_BUCKETS.map((option) => option.value)),
    kind: enumParam(url.searchParams.get('kind'), RECOVERY_WORK_KINDS.map((option) => option.value)),
    source: cleanParam(url.searchParams.get('source'), 255),
    stage: enumParam(url.searchParams.get('stage'), RECOVERY_STAGES.map((option) => option.value)),
    reason: cleanParam(url.searchParams.get('reason'), 128),
    query: cleanParam(url.searchParams.get('q'), 255),
    cursor: cleanParam(url.searchParams.get('cursor'), 2048),
    jobCursor: cleanParam(url.searchParams.get('job_cursor'), 2048)
  };
}

export function recoveryHref(
  filters: RecoveryFilters,
  overrides: Partial<RecoveryFilters> = {}
): string {
  const next = { ...filters, ...overrides };
  const params = new URLSearchParams();
  if (next.section !== 'needs_attention') setParam(params, 'view', next.section);
  setParam(params, 'bucket', next.bucket);
  setParam(params, 'kind', next.kind);
  setParam(params, 'source', next.source);
  setParam(params, 'stage', next.stage);
  setParam(params, 'reason', next.reason);
  setParam(params, 'q', next.query);
  setParam(params, 'cursor', next.cursor);
  setParam(params, 'job_cursor', next.jobCursor);
  const query = params.toString();
  return query ? `/admin/recovery?${query}` : '/admin/recovery';
}

export function recoveryWorkHref(work: Pick<AdminRecoveryWorkRead, 'kind' | 'id'>): string {
  return `/admin/recovery/work/${encodeURIComponent(work.kind)}/${encodeURIComponent(work.id)}`;
}

export function recoveryBucketLabel(bucket: AdminRecoveryBucket): string {
  return RECOVERY_BUCKETS.find((option) => option.value === bucket)?.label ?? humanizeRecoveryValue(bucket);
}

export function recoveryWorkKindLabel(kind: AdminRecoveryWorkKind): string {
  return RECOVERY_WORK_KINDS.find((option) => option.value === kind)?.label ?? humanizeRecoveryValue(kind);
}

export function recoveryCapabilityLabel(capability: AdminRecoveryCapability): string {
  const labels: Record<AdminRecoveryCapability, string> = {
    resume_backfill: 'Resume backfill',
    replay_source_post: 'Replay post',
    reinspect_ingest: 'Re-inspect media',
    regenerate_derivatives: 'Regenerate derivatives',
    replay_stage: 'Replay stage',
    retry_stage: 'Retry stage',
    resync_target: 'Resync target',
    rebuild_outbox: 'Rebuild event',
    recover_dead_letter: 'Recover dead letter',
    archive_dead_letter: 'Archive dead letter'
  };
  return labels[capability];
}

export function recoveryPrimaryCapability(
  capabilities: AdminRecoveryCapability[]
): AdminRecoveryCapability | null {
  const priority: AdminRecoveryCapability[] = [
    'regenerate_derivatives',
    'replay_stage',
    'resume_backfill',
    'replay_source_post',
    'reinspect_ingest',
    'retry_stage',
    'resync_target',
    'rebuild_outbox',
    'recover_dead_letter',
    'archive_dead_letter'
  ];
  return priority.find((capability) => capabilities.includes(capability)) ?? null;
}

export function recoveryDefaultBatchCapability(
  work: Pick<AdminRecoveryWorkRead, 'capabilities'>[]
): AdminRecoveryCapability {
  for (const item of work) {
    const capability = recoveryPrimaryCapability(item.capabilities);
    if (capability) return capability;
  }
  return 'retry_stage';
}

export function recoveryActionsForWork(
  work: Pick<AdminRecoveryWorkRead, 'actions' | 'available_actions' | 'capabilities'>
): AdminRecoveryActionCandidateRead[] {
  const declared = work.actions?.length ? work.actions : work.available_actions;
  if (declared?.length) return declared.map(normalizeRecoveryAction);
  return work.capabilities.map((capability) => normalizeRecoveryAction({ capability, available: true }));
}

export function recoveryBatchAcknowledgements(
  work: Pick<AdminRecoveryWorkRead, 'actions' | 'available_actions' | 'capabilities'>[],
  capability: AdminRecoveryCapability,
  scope: AdminRecoveryReplayScope
): AdminRecoveryAcknowledgementRead[] {
  const acknowledgements = new Map<string, AdminRecoveryAcknowledgementRead>();
  for (const item of work) {
    const action = recoveryActionsForWork(item).find(
      (candidate) => candidate.available && candidate.capability === capability
    );
    if (!action) continue;
    for (const acknowledgement of recoveryActionRequirements(action, scope).required_acknowledgements) {
      acknowledgements.set(acknowledgement.key, acknowledgement);
    }
  }
  return [...acknowledgements.values()];
}

export function normalizeRecoveryAction(
  action: AdminRecoveryActionCandidateRead
): AdminRecoveryActionCandidateRead {
  const capability = action.capability ?? action.action;
  return {
    ...action,
    capability,
    available: action.available ?? true,
    scopes: action.scopes?.length ? action.scopes : ['stage_only'],
    default_scope: action.default_scope ?? action.scopes?.[0] ?? 'stage_only',
    retry_limits: action.retry_limits?.length ? action.retry_limits : RECOVERY_RETRY_LIMITS,
    default_retry_limit: action.default_retry_limit ?? 3,
    downstream_stages: action.downstream_stages ?? [],
    warnings: action.warnings ?? [],
    risks: action.risks ?? [],
    required_acknowledgements: (action.required_acknowledgements ?? []).map(recoveryAcknowledgement),
    scope_requirements: action.scope_requirements ?? {},
    blocked_prerequisites: action.blocked_prerequisites ?? []
  };
}

export function recoveryActionRequirements(
  action: AdminRecoveryActionCandidateRead,
  scope: AdminRecoveryReplayScope
): {
  warnings: string[];
  risks: NonNullable<AdminRecoveryActionCandidateRead['risks']>;
  required_acknowledgements: AdminRecoveryAcknowledgementRead[];
} {
  const scoped = action.scope_requirements?.[scope];
  const warnings = scoped ? scoped.warnings ?? [] : action.warnings ?? [];
  const risks = scoped ? scoped.risks ?? [] : action.risks ?? [];
  const acknowledgements = scoped
    ? scoped.required_acknowledgements ?? []
    : action.required_acknowledgements ?? [];
  return {
    warnings,
    risks,
    required_acknowledgements: acknowledgements.map(recoveryAcknowledgement)
  };
}

export function recoveryDefaultScope(action: AdminRecoveryActionCandidateRead): AdminRecoveryReplayScope {
  return action.default_scope ?? action.scopes?.[0] ?? 'stage_only';
}

export function recoveryDefaultRetryLimit(action: AdminRecoveryActionCandidateRead): AdminRecoveryRetryLimit {
  return action.default_retry_limit ?? 3;
}

export function recoverySafeMessage(value: { message: string } | string): string {
  return typeof value === 'string' ? value : value.message;
}

export function recoveryAcknowledgement(
  value: AdminRecoveryAcknowledgementRead | string
): AdminRecoveryAcknowledgementRead {
  if (typeof value !== 'string') return value;
  const labels: Record<string, string> = {
    terminal_override: 'I acknowledge that this terminal failure is being overridden for an audited replay.',
    stale_dependents: 'I acknowledge that stage-only replay may leave existing dependent data stale.',
    media_replacement: 'I acknowledge that verified web media will replace the active derivative atomically.'
  };
  return { key: value, label: labels[value] ?? humanizeRecoveryValue(value) };
}

export function humanizeRecoveryValue(value: string | null): string {
  if (!value) return 'Not available';
  return value
    .replaceAll('_', ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function enumParam<T extends string>(value: string | null, allowed: readonly T[]): T | null {
  const normalized = cleanParam(value);
  return normalized && allowed.includes(normalized as T) ? (normalized as T) : null;
}

function cleanParam(value: string | null, maxLength = 200): string | null {
  const normalized = value?.trim();
  return normalized ? normalized.slice(0, maxLength) : null;
}

function setParam(params: URLSearchParams, name: string, value: string | null): void {
  if (value) params.set(name, value);
}
