import type { MemeResultAttributionRead } from '$lib/api/types';

export interface MemeDiscoveryDataAttributes {
  'data-discovery-source': string | undefined;
  'data-discovery-reason': string | undefined;
  'data-discovery-request-id': string | undefined;
  'data-discovery-impression-id': string | undefined;
  'data-discovery-source-meme-id': string | undefined;
  'data-discovery-score': number | undefined;
}

export function memeDiscoveryDataAttributes(
  attribution: MemeResultAttributionRead | null | undefined
): MemeDiscoveryDataAttributes {
  return {
    'data-discovery-source': attribution?.source_algorithm ?? undefined,
    'data-discovery-reason': attribution?.reason ?? undefined,
    'data-discovery-request-id': attribution?.request_id ?? undefined,
    'data-discovery-impression-id': attribution?.impression_id ?? undefined,
    'data-discovery-source-meme-id': attribution?.source_meme_id ?? undefined,
    'data-discovery-score': attribution?.score ?? undefined
  };
}
