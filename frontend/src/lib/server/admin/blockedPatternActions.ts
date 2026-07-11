import type { RequestEvent } from '@sveltejs/kit';
import {
  createBlockedPerceptualHash as createBlockedPatternRequest,
  deactivateBlockedPerceptualHash as deactivateBlockedPatternRequest,
  deleteBlockedPerceptualHash as deleteBlockedPatternRequest,
  updateBlockedPerceptualHash as updateBlockedPatternRequest
} from '$lib/api/client';
import { buildBlockedPhashPayload } from '$lib/admin/blockedPhash';
import { apiRequest, readInt, readOptional, readRequired, requireConfirmation, runAction } from './actionUtils';

export async function createBlockedPerceptualHash({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    await createBlockedPatternRequest({
      ...apiRequest(fetch, request),
      body: blockedPhashPayloadFromForm(data)
    });
    return { message: 'Blocked media pattern created.' };
  });
}

export async function updateBlockedPerceptualHash({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    await updateBlockedPatternRequest(
      {
        ...apiRequest(fetch, request),
        body: blockedPhashPayloadFromForm(data)
      },
      readRequired(data, 'blocked_hash_id')
    );
    return { message: 'Blocked media pattern updated.' };
  });
}

export async function deactivateBlockedPerceptualHash({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    requireConfirmation(
      readRequired(data, 'confirmation_phrase'),
      'DEACTIVATE',
      'Type DEACTIVATE to confirm this action.'
    );
    await deactivateBlockedPatternRequest(
      {
        ...apiRequest(fetch, request),
        body: { note: readOptional(data, 'note') }
      },
      readRequired(data, 'blocked_hash_id')
    );
    return { message: 'Blocked media pattern deactivated.' };
  });
}

export async function reactivateBlockedPerceptualHash({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    requireConfirmation(
      readRequired(data, 'confirmation_phrase'),
      'REACTIVATE',
      'Type REACTIVATE to confirm this action.'
    );
    await updateBlockedPatternRequest(
      {
        ...apiRequest(fetch, request),
        body: { is_active: true }
      },
      readRequired(data, 'blocked_hash_id')
    );
    return { message: 'Blocked media pattern reactivated.' };
  });
}

export async function deleteBlockedPerceptualHash({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    requireConfirmation(readRequired(data, 'confirmation_phrase'), 'DELETE', 'Type DELETE to confirm this action.');
    const result = await deleteBlockedPatternRequest(
      apiRequest(fetch, request),
      readRequired(data, 'blocked_hash_id')
    );
    return {
      message:
        result.action === 'deactivate'
          ? 'Blocked media pattern deactivated because quarantined files still reference it.'
          : 'Blocked media pattern deleted; audit history preserved.'
    };
  });
}

function blockedPhashPayloadFromForm(data: FormData) {
  return buildBlockedPhashPayload({
    perceptualHash: readRequired(data, 'perceptual_hash'),
    hashAlgorithm: readOptional(data, 'hash_algorithm'),
    maxHammingDistance: readInt(data, 'max_hamming_distance', 0),
    reason: readRequired(data, 'reason'),
    note: readOptional(data, 'note'),
    isActive: data.get('is_active') === 'on'
  });
}

export const blockedPatternActions = {
  createBlockedPerceptualHash,
  updateBlockedPerceptualHash,
  deactivateBlockedPerceptualHash,
  reactivateBlockedPerceptualHash,
  deleteBlockedPerceptualHash
};
