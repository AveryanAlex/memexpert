import { describe, expect, it } from 'vitest';

import type { MemeResultAttributionRead } from '$lib/api/types';
import { memeDiscoveryDataAttributes } from './discovery-attribution';

describe('memeDiscoveryDataAttributes', () => {
  it('maps every shared discovery field without dropping a zero score', () => {
    const attribution: MemeResultAttributionRead = {
      attribution_token: null,
      candidate_sources: [],
      profile_version: null,
      request_id: 'request-1',
      impression_id: 'impression-1',
      surface: 'web_home',
      source_algorithm: 'related',
      rank: 1,
      query: null,
      filters: { language: null, media_type: null, include_nsfw: false, tags: [], scope: 'public', collection_ids: [] },
      collection_scope: 'public',
      collection_ids: [],
      source_meme_id: 'source-meme-1',
      algorithm_version: 'test',
      score: 0,
      score_components: {},
      reason: 'similarity'
    };

    expect(memeDiscoveryDataAttributes(attribution)).toEqual({
      'data-discovery-source': 'related',
      'data-discovery-reason': 'similarity',
      'data-discovery-request-id': 'request-1',
      'data-discovery-impression-id': 'impression-1',
      'data-discovery-source-meme-id': 'source-meme-1',
      'data-discovery-score': 0
    });
  });

  it('leaves unavailable discovery fields undefined for DOM omission', () => {
    expect(memeDiscoveryDataAttributes(null)).toEqual({
      'data-discovery-source': undefined,
      'data-discovery-reason': undefined,
      'data-discovery-request-id': undefined,
      'data-discovery-impression-id': undefined,
      'data-discovery-source-meme-id': undefined,
      'data-discovery-score': undefined
    });
  });
});
