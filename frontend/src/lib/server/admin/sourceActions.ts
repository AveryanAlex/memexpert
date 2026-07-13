import type { RequestEvent } from '@sveltejs/kit';
import {
  ApiError,
  addAdminTelegramChannelFromReference,
  addSourceChannel as createSourceChannel,
  assignAdminTelegramChannel,
  backfillAdminSourceChannel,
  markSourceChannelDead as markSourceChannelDeadRequest,
  orphanAdminTelegramChannel,
  reviewChannelSuggestion,
  setSourceChannelPaused,
  updateAdminTelegramChannel,
  validateAdminTelegramSession
} from '$lib/api/client';
import { apiRequest, readBoolean, readInt, readOptional, readRequired, requireConfirmation, runAction } from './actionUtils';

export async function reviewSuggestion({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const suggestionId = readRequired(data, 'suggestion_id');
    const decision = readRequired(data, 'decision');
    if (decision !== 'approve' && decision !== 'reject') throw new ApiError(400, 'Unknown review decision.');
    await reviewChannelSuggestion(
      {
        ...apiRequest(fetch, request),
        body: { admin_note: readOptional(data, 'admin_note') }
      },
      suggestionId,
      decision
    );
    return { message: `Suggestion ${decision === 'approve' ? 'approved' : 'rejected'}.` };
  });
}

export async function addSourceChannel({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const platform = readRequired(data, 'platform');
    if (platform !== 'telegram') throw new ApiError(400, 'Only Telegram sources can be added until crawler support is available.');
    await createSourceChannel({
      ...apiRequest(fetch, request),
      body: {
        platform,
        platform_id: readRequired(data, 'platform_id'),
        username: readOptional(data, 'username'),
        title: readRequired(data, 'title'),
        orphaned: true,
        catchup_message_limit: 5000,
        catchup_enabled: false,
        live_enabled: false,
        engagement_enabled: false
      }
    });
    return { message: 'Source added without an account; ingestion is off.' };
  });
}

export async function addSourceByReference({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const suggestionId = readOptional(data, 'suggestion_id');
    await addAdminTelegramChannelFromReference({
      ...apiRequest(fetch, request),
      body: {
        reference: readRequired(data, 'reference'),
        telegram_session_id: readRequired(data, 'telegram_session_id'),
        suggestion_id: suggestionId,
        catchup_message_limit: readInt(data, 'catchup_message_limit', 5000)
      }
    });
    return {
      message: suggestionId
        ? 'Telegram source added and suggestion approved.'
        : 'Telegram source added and ready to fetch.'
    };
  });
}

export async function toggleSourceChannel({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const channelId = readRequired(data, 'channel_id');
    const paused = readBoolean(data, 'paused');
    await setSourceChannelPaused(
      apiRequest(fetch, request),
      channelId,
      paused
    );
    return { message: paused ? 'Source paused.' : 'Source resumed.' };
  });
}

export async function markSourceChannelDead({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const channelId = readRequired(data, 'channel_id');
    const confirmation = readRequired(data, 'confirmation');
    requireConfirmation(confirmation, channelId, 'Paste the source ID from Diagnostics to remove this source.');
    await markSourceChannelDeadRequest(apiRequest(fetch, request), channelId, confirmation);
    return { message: 'Source removed from crawling; checkpoint history was preserved.' };
  });
}

export async function updateSourceChannelIngestion({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const channelId = readRequired(data, 'channel_id');
    await updateAdminTelegramChannel(
      {
        ...apiRequest(fetch, request),
        body: {
          catchup_enabled: data.get('catchup_enabled') === 'on',
          live_enabled: data.get('live_enabled') === 'on',
          engagement_enabled: data.get('engagement_enabled') === 'on',
          catchup_message_limit: readInt(data, 'catchup_message_limit', 5000)
        }
      },
      channelId
    );
    return { message: 'Source ingestion settings updated.' };
  });
}

export async function assignSourceChannel({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const channelId = readRequired(data, 'channel_id');
    await assignAdminTelegramChannel(
      {
        ...apiRequest(fetch, request),
        body: {
          telegram_session_id: readRequired(data, 'telegram_session_id'),
          note: readOptional(data, 'note')
        }
      },
      channelId
    );
    return { message: 'Source assigned to a Telegram account.' };
  });
}

export async function orphanSourceChannel({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const channelId = readRequired(data, 'channel_id');
    await orphanAdminTelegramChannel(
      {
        ...apiRequest(fetch, request),
        body: { note: readOptional(data, 'note') }
      },
      channelId
    );
    return { message: 'Source is now unassigned and ingestion is off.' };
  });
}

export async function validateSourceAccount({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const telegramSessionId = readRequired(data, 'telegram_session_id');
    const sourceChannelId = readRequired(data, 'source_channel_id');
    const result = await validateAdminTelegramSession(
      {
        ...apiRequest(fetch, request),
        body: { source_channel_id: sourceChannelId, note: readOptional(data, 'note') }
      },
      telegramSessionId
    );
    return {
      message: result.channel_checked
        ? `Source access validated with ${result.channel_reference ?? 'the selected source'}.`
        : 'Telegram account validated for this source.'
    };
  });
}

export async function backfillSourceChannel({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const channelId = readRequired(data, 'channel_id');
    const messageLimit = readInt(data, 'message_limit', 5000);
    if (messageLimit < 1 || messageLimit > 50_000) {
      throw new ApiError(400, 'message_limit must be between 1 and 50000.');
    }
    await backfillAdminSourceChannel(
      {
        ...apiRequest(fetch, request),
        body: { message_limit: messageLimit }
      },
      channelId
    );
    return { message: `Older-message backfill queued for ${messageLimit.toLocaleString('en-US')} messages.` };
  });
}

export const sourceActions = {
  reviewSuggestion,
  addSourceByReference,
  addSourceChannel,
  toggleSourceChannel,
  markSourceChannelDead,
  updateSourceChannelIngestion,
  assignSourceChannel,
  orphanSourceChannel,
  validateSourceAccount,
  backfillSourceChannel
};
