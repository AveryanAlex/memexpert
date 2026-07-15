import type {
  AdminRecoveryBucket,
  AdminRecoveryCapability,
  AdminRecoveryWorkKind,
  AdminRecoveryWorkRead
} from '$lib/api/types';

export const RECOVERY_PAGE_SIZE = 50;

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
  bucket: AdminRecoveryBucket | null;
  kind: AdminRecoveryWorkKind | null;
  source: string | null;
  stage: string | null;
  reason: string | null;
  query: string | null;
  cursor: string | null;
}

export interface RecoveryWorkspaceRequestIds {
  batchPreview: string;
  work: Record<string, string>;
}

export function recoveryWorkRequestKey(work: Pick<AdminRecoveryWorkRead, 'kind' | 'id'>): string {
  return `${work.kind}:${work.id}`;
}

export function recoveryFiltersFromUrl(url: URL): RecoveryFilters {
  return {
    bucket: enumParam(url.searchParams.get('bucket'), RECOVERY_BUCKETS.map((option) => option.value)),
    kind: enumParam(url.searchParams.get('kind'), RECOVERY_WORK_KINDS.map((option) => option.value)),
    source: cleanParam(url.searchParams.get('source'), 255),
    stage: enumParam(url.searchParams.get('stage'), RECOVERY_STAGES.map((option) => option.value)),
    reason: cleanParam(url.searchParams.get('reason'), 128),
    query: cleanParam(url.searchParams.get('q'), 255),
    cursor: cleanParam(url.searchParams.get('cursor'), 2048)
  };
}

export function recoveryHref(
  filters: RecoveryFilters,
  overrides: Partial<RecoveryFilters> = {}
): string {
  const next = { ...filters, ...overrides };
  const params = new URLSearchParams();
  setParam(params, 'bucket', next.bucket);
  setParam(params, 'kind', next.kind);
  setParam(params, 'source', next.source);
  setParam(params, 'stage', next.stage);
  setParam(params, 'reason', next.reason);
  setParam(params, 'q', next.query);
  setParam(params, 'cursor', next.cursor);
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
