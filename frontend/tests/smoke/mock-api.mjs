import { createServer } from 'node:http';

const port = Number(process.env.PORT ?? 8787);
const seededCollectionId = 'smoke-private-team-saves';
const adminAccessTokenPrefix = 'smoke-admin-';

const meme = {
  id: 'smoke-meme-1',
  media_type: 'image',
  language: 'en',
  is_nsfw: false,
  popularity_score: 42.5,
  like_count: 7,
  tags: ['cat', 'reaction', 'smoke'],
  primary_file: {
    id: 'smoke-file-1',
    mime_type: 'image/svg+xml',
    width: 640,
    height: 360,
    file_size_bytes: 512,
    blur_hash: null,
    quality_score: 0.99,
    render: {
      thumbnail_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
      preview_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
      display_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
      original_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
      download_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
      web_video_url: null,
      width: 640,
      height: 360,
      blur_hash: null
    },
    render_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
    download_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`
  },
  caption: 'Smoke test cat reaction',
  seo_page_slug: 'smoke-test-cat-reaction',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  render_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
  download_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
  viewer_has_favorited: false,
  viewer_has_saved: false,
  viewer_has_pinned: false
};

const detail = {
  ...meme,
  ocr_text: 'cat says ship it',
  seo_title: 'Smoke test cat reaction',
  seo_description: 'A deterministic smoke-test meme served by the local mocked backend.',
  seo_alt_text: 'A cat reaction smoke test graphic',
  seo_body_text: 'Used by Playwright to verify search, detail, media, and action visibility.',
  seo_model_id: null,
  seo_prompt_version: null,
  seo_generated_at: null,
  files: [meme.primary_file]
};

const nextMeme = {
  ...meme,
  id: 'smoke-meme-2',
  caption: 'Smoke test deploy mood',
  seo_page_slug: 'smoke-test-deploy-mood',
  tags: ['deploy', 'smoke'],
  primary_file: {
    ...meme.primary_file,
    id: 'smoke-file-2'
  }
};

const collectionMeme = {
  ...meme,
  id: 'smoke-meme-collection-1',
  caption: 'Smoke test vault reaction',
  seo_page_slug: 'smoke-test-vault-reaction',
  tags: ['vault', 'reaction', 'collection'],
  primary_file: {
    ...meme.primary_file,
    id: 'smoke-file-collection-1'
  },
  viewer_access: { visibility: 'shared' }
};

const seededCollection = {
  id: seededCollectionId,
  owner_id: 'smoke-owner-user',
  title: 'Smoke private team saves',
  description: 'Deterministic collection used by browser smoke tests.',
  kind: 'custom',
  visibility: 'private',
  memberships: [],
  invites: [],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z'
};

const seededCollectionSummary = {
  collection: seededCollection,
  viewer_role: 'viewer',
  capabilities: {
    can_view: true,
    can_add_memes: false,
    can_remove_memes: false,
    can_rename: false,
    can_delete: false,
    can_create_invites: false,
    can_revoke_invites: false,
    can_manage_members: false,
    can_set_active_save: true
  },
  active_save_collection_id: seededCollectionId
};

const trend = {
  recent: { views: 120, sends: 8, likes: 7, saves: 4, downloads: 3 },
  previous: { views: 90, sends: 5, likes: 4, saves: 2, downloads: 1 },
  latest_snapshot_at: '2026-01-01T00:00:00Z',
  latest_source_views: 120,
  latest_source_reactions: 7,
  latest_source_reposts: 8,
  latest_platform_views: 120,
  latest_platform_sends: 8,
  latest_platform_saves: 4,
  latest_platform_likes: 7,
  latest_popularity_score: 42.5,
  engagement_24h: 22,
  trending_score: 42.5,
  refreshed_at: '2026-01-01T00:00:00Z'
};

const motdAlgorithmVersion = 'motd-smoke-v1';
const motdScoreComponents = { popularity: 0.42, quality: 0.55 };
const motdScore = 0.97;

const adminIds = {
  user: '1cb7b083-dc9f-45a6-9e4c-3dc497651a01',
  suggester: '1cb7b083-dc9f-45a6-9e4c-3dc497651a02',
  reporter: '1cb7b083-dc9f-45a6-9e4c-3dc497651a03',
  meme: '1cb7b083-dc9f-45a6-9e4c-3dc497651a04',
  mediaFile: '1cb7b083-dc9f-45a6-9e4c-3dc497651a05',
  readyAccount: '1cb7b083-dc9f-45a6-9e4c-3dc497651a06',
  floodWaitAccount: '1cb7b083-dc9f-45a6-9e4c-3dc497651a07',
  healthySource: '1cb7b083-dc9f-45a6-9e4c-3dc497651a08',
  orphanedSource: '1cb7b083-dc9f-45a6-9e4c-3dc497651a09',
  staleSource: '1cb7b083-dc9f-45a6-9e4c-3dc497651a10',
  quickAddedSource: '1cb7b083-dc9f-45a6-9e4c-3dc497651a11',
  telegramSuggestion: '1cb7b083-dc9f-45a6-9e4c-3dc497651a12',
  redditSuggestion: '1cb7b083-dc9f-45a6-9e4c-3dc497651a13',
  vkSuggestion: '1cb7b083-dc9f-45a6-9e4c-3dc497651a14',
  report: '1cb7b083-dc9f-45a6-9e4c-3dc497651a15',
  decision: '1cb7b083-dc9f-45a6-9e4c-3dc497651a16',
  blockedPattern: '1cb7b083-dc9f-45a6-9e4c-3dc497651a17',
  curatedTemplate: '1cb7b083-dc9f-45a6-9e4c-3dc497651a18',
  uncuratedTemplate: '1cb7b083-dc9f-45a6-9e4c-3dc497651a19'
};

const adminMediaPreviewUrl = `/api/v1/media/files/${adminIds.mediaFile}/preview`;
const adminMediaFile = {
  ...meme.primary_file,
  id: adminIds.mediaFile,
  render: {
    ...meme.primary_file.render,
    thumbnail_url: adminMediaPreviewUrl,
    preview_url: adminMediaPreviewUrl,
    display_url: adminMediaPreviewUrl,
    original_url: adminMediaPreviewUrl,
    download_url: adminMediaPreviewUrl
  },
  render_url: adminMediaPreviewUrl,
  download_url: adminMediaPreviewUrl
};

const adminOverview = {
  open_report_count: 1,
  pending_suggestion_count: 3,
  source_attention_count: 2,
  orphaned_source_count: 1,
  stale_source_count: 2,
  waiting_source_count: 0,
  healthy_source_count: 1,
  telegram_account_attention_count: 1,
  ready_telegram_account_count: 1,
  missing_seo_count: 1,
  uncurated_template_count: 1
};

const adminMeme = {
  ...meme,
  id: adminIds.meme,
  primary_file: adminMediaFile,
  is_public: false,
  template_id: adminIds.curatedTemplate,
  author_user_id: null
};

const readyAdminTelegramAccount = {
  id: adminIds.readyAccount,
  name: 'crawler-alpha',
  display_name: 'Meme desk account',
  owned_channel_count: 2,
  status: 'active',
  enabled: true,
  flood_wait_until: null,
  live_listener_started_at: '2026-01-01T00:00:00Z',
  last_heartbeat_at: '2026-01-01T00:04:00Z',
  last_error_class: null,
  last_error_text: null,
  quarantined_at: null,
  live_enabled: true,
  catchup_enabled: true,
  engagement_enabled: true,
  max_requests_per_second: 1,
  account_user_id: 4242,
  account_username: 'meme_ops',
  account_phone_hint: null,
  has_string_session: true,
  created_at: '2025-12-01T00:00:00Z',
  updated_at: '2026-01-01T00:04:00Z'
};

const floodWaitAdminTelegramAccount = {
  ...readyAdminTelegramAccount,
  id: adminIds.floodWaitAccount,
  name: 'crawler-rate-limited',
  display_name: 'Rate-limited account',
  owned_channel_count: 0,
  account_username: 'meme_backup',
  flood_wait_until: '2099-01-01T00:00:00Z'
};

const adminTelegramAccounts = [readyAdminTelegramAccount, floodWaitAdminTelegramAccount];
const adminSourceSeed = [
  {
    id: adminIds.healthySource,
    platform: 'telegram',
    platform_id: 'daily_cats',
    username: 'daily_cats',
    title: 'Daily cats',
    subscriber_count: 1200,
    is_active: true,
    is_paused: false,
    catchup_enabled: true,
    live_enabled: true,
    engagement_enabled: true,
    catchup_message_limit: 5000,
    telegram_session_id: readyAdminTelegramAccount.id,
    telegram_session_name: readyAdminTelegramAccount.name,
    is_orphaned: false,
    is_indexable: true,
    last_read_post_id: '184',
    oldest_observed_post_id: '160',
    initial_catchup_completed: true,
    history_exhausted: false,
    backfill_status: 'idle',
    backfill_requested_count: 0,
    backfill_scanned_count: 0,
    backfill_error: null,
    last_fetched_at: '2026-01-01T00:00:00Z',
    operational_status: 'active',
    freshness_status: 'fresh',
    seconds_since_last_fetch: 240,
    created_at: '2025-12-20T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  },
  {
    id: adminIds.orphanedSource,
    platform: 'telegram',
    platform_id: 'small_memes',
    username: 'small_memes',
    title: 'Small memes',
    subscriber_count: null,
    is_active: true,
    is_paused: false,
    catchup_enabled: false,
    live_enabled: false,
    engagement_enabled: false,
    catchup_message_limit: 5000,
    telegram_session_id: null,
    telegram_session_name: null,
    is_orphaned: true,
    is_indexable: false,
    last_read_post_id: null,
    oldest_observed_post_id: null,
    initial_catchup_completed: false,
    history_exhausted: false,
    backfill_status: 'idle',
    backfill_requested_count: 0,
    backfill_scanned_count: 0,
    backfill_error: null,
    last_fetched_at: null,
    operational_status: 'active',
    freshness_status: 'never_fetched',
    seconds_since_last_fetch: null,
    created_at: '2025-12-20T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  },
  {
    id: adminIds.staleSource,
    platform: 'telegram',
    platform_id: 'retro_memes',
    username: 'retro_memes',
    title: 'Retro memes',
    subscriber_count: 410,
    is_active: true,
    is_paused: false,
    catchup_enabled: true,
    live_enabled: true,
    engagement_enabled: true,
    catchup_message_limit: 5000,
    telegram_session_id: readyAdminTelegramAccount.id,
    telegram_session_name: readyAdminTelegramAccount.name,
    is_orphaned: false,
    is_indexable: true,
    last_read_post_id: '18',
    oldest_observed_post_id: '4',
    initial_catchup_completed: true,
    history_exhausted: false,
    backfill_status: 'failed',
    backfill_requested_count: 5000,
    backfill_scanned_count: 121,
    backfill_error: 'Telegram temporarily refused the history request.',
    last_fetched_at: '2025-12-25T00:00:00Z',
    operational_status: 'active',
    freshness_status: 'stale',
    seconds_since_last_fetch: 604800,
    created_at: '2025-12-10T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  }
];
const adminSourceStateBySession = new Map();
const loginAttemptPolls = new Map();
const staleFullSessionReads = new Set();
let loginAttemptSequence = 0;

const adminSourcePosts = [
  {
    id: 'source-post-indexed',
    post_id: '184',
    telegram_url: 'https://t.me/daily_cats/184',
    published_at: '2026-01-01T00:00:00Z',
    observed_at: '2026-01-01T00:00:10Z',
    media_type: 'image',
    fetch_status: 'accepted',
    fetch_detail: null,
    ingest_outcome: 'ingested',
    ingest_status: 'materialized',
    meme_id: adminIds.meme,
    meme_file_id: adminIds.mediaFile,
    pipeline_stage: 'sync_meili',
    pipeline_status: 'succeeded',
    pipeline_error: null,
    qdrant_status: 'synced',
    meilisearch_status: 'synced',
    index_status: 'indexed'
  },
  {
    id: 'source-post-partial',
    post_id: '183',
    telegram_url: 'https://t.me/daily_cats/183',
    published_at: '2025-12-31T23:00:00Z',
    observed_at: '2026-01-01T00:00:11Z',
    media_type: 'video',
    fetch_status: 'accepted',
    fetch_detail: null,
    ingest_outcome: 'ingested',
    ingest_status: 'materialized',
    meme_id: adminIds.meme,
    meme_file_id: adminIds.mediaFile,
    pipeline_stage: 'sync_meili',
    pipeline_status: 'processing',
    pipeline_error: null,
    qdrant_status: 'synced',
    meilisearch_status: 'processing',
    index_status: 'partially_indexed'
  },
  {
    id: 'source-post-processing',
    post_id: '182',
    telegram_url: 'https://t.me/daily_cats/182',
    published_at: '2025-12-31T22:00:00Z',
    observed_at: '2026-01-01T00:00:12Z',
    media_type: 'image',
    fetch_status: 'accepted',
    fetch_detail: null,
    ingest_outcome: 'ingested',
    ingest_status: 'media_inspecting',
    meme_id: null,
    meme_file_id: null,
    pipeline_stage: null,
    pipeline_status: null,
    pipeline_error: null,
    qdrant_status: null,
    meilisearch_status: null,
    index_status: 'processing'
  },
  {
    id: 'source-post-failed',
    post_id: '181',
    telegram_url: 'https://t.me/daily_cats/181',
    published_at: '2025-12-31T21:00:00Z',
    observed_at: '2026-01-01T00:00:13Z',
    media_type: 'image',
    fetch_status: 'accepted',
    fetch_detail: null,
    ingest_outcome: 'ingested',
    ingest_status: 'materialized',
    meme_id: adminIds.meme,
    meme_file_id: adminIds.mediaFile,
    pipeline_stage: 'embed',
    pipeline_status: 'failed',
    pipeline_error: 'Embedding provider unavailable.',
    qdrant_status: 'failed',
    meilisearch_status: 'pending',
    index_status: 'failed'
  },
  {
    id: 'source-post-skipped',
    post_id: '180',
    telegram_url: 'https://t.me/daily_cats/180',
    published_at: '2025-12-31T20:00:00Z',
    observed_at: '2026-01-01T00:00:14Z',
    media_type: 'text',
    fetch_status: 'unsupported',
    fetch_detail: 'The message has no supported meme media.',
    ingest_outcome: 'skipped_unsupported_media',
    ingest_status: null,
    meme_id: null,
    meme_file_id: null,
    pipeline_stage: null,
    pipeline_status: null,
    pipeline_error: null,
    qdrant_status: null,
    meilisearch_status: null,
    index_status: 'not_indexable'
  }
];

const adminSuggestions = [
  {
    id: adminIds.telegramSuggestion,
    user_id: adminIds.suggester,
    platform: 'telegram',
    channel_url: 'https://t.me/pizza_memes',
    status: 'pending',
    admin_note: 'Popular local suggestion.',
    reviewed_at: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  },
  {
    id: adminIds.redditSuggestion,
    user_id: adminIds.suggester,
    platform: 'reddit',
    channel_url: 'https://reddit.com/r/memes',
    status: 'pending',
    admin_note: null,
    reviewed_at: null,
    created_at: '2025-12-31T00:00:00Z',
    updated_at: '2025-12-31T00:00:00Z'
  },
  {
    id: adminIds.vkSuggestion,
    user_id: adminIds.suggester,
    platform: 'vk',
    channel_url: 'https://vk.com/memes',
    status: 'pending',
    admin_note: null,
    reviewed_at: null,
    created_at: '2025-12-30T00:00:00Z',
    updated_at: '2025-12-30T00:00:00Z'
  }
];

const adminReport = {
  id: adminIds.report,
  meme_id: adminMeme.id,
  reporter_user_id: adminIds.reporter,
  status: 'pending',
  reason: 'spam',
  note: 'This looks unrelated to the source channel.',
  resolved_by_admin_user_id: null,
  resolved_at: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  meme: adminMeme
};

const adminDecision = {
  id: adminIds.decision,
  meme_id: adminMeme.id,
  report_id: null,
  admin_user_id: adminIds.user,
  action: 'mark_sfw',
  reason: 'other',
  note: 'Previous review kept this visible.',
  previous_is_public: true,
  previous_is_nsfw: true,
  new_is_public: true,
  new_is_nsfw: false,
  previous_template_id: null,
  new_template_id: adminIds.curatedTemplate,
  created_at: '2025-12-31T00:00:00Z'
};

const adminBlockedPattern = {
  id: adminIds.blockedPattern,
  perceptual_hash: 'f0e1d2c3b4a59687',
  hash_algorithm: 'phash',
  hash_size: 64,
  max_hamming_distance: 4,
  reason: 'spam',
  note: 'Known reposted artwork.',
  is_active: true,
  created_by_admin_user_id: adminIds.user,
  created_at: '2025-12-20T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z'
};

const adminSeoPage = {
  meme_id: adminMeme.id,
  slug: 'smoke-admin-meme',
  page_title: 'Smoke admin meme',
  meta_description: 'A representative SEO review row for deterministic browser coverage.',
  alt_text: 'A smoke-test meme graphic.',
  caption: 'Check the queue item before publishing.',
  body_text: 'This row is served by the local smoke API.',
  tags: ['smoke', 'review'],
  model_id: 'smoke-provider',
  prompt_version: 'smoke-v1',
  generated_at: '2026-01-01T00:00:00Z',
  edited_at: null
};

const adminTemplates = [
  {
    id: adminIds.curatedTemplate,
    slug: 'ship-it-cat',
    name: 'Ship it cat',
    description: 'A cat reaction for a successful launch.',
    is_curated: true,
    base_image_url: null,
    text_regions: [],
    created_at: '2025-12-20T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  },
  {
    id: adminIds.uncuratedTemplate,
    slug: 'launch-panic',
    name: 'Launch panic',
    description: null,
    is_curated: false,
    base_image_url: null,
    text_regions: null,
    created_at: '2025-12-31T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  }
];

const server = createServer((request, response) => {
  const url = new URL(request.url ?? '/', `http://${request.headers.host ?? `127.0.0.1:${port}`}`);

  if (url.pathname === '/health') {
    sendJson(response, 200, { ok: true });
    return;
  }

  if (url.pathname === '/media/smoke-cat.svg') {
    response.writeHead(200, {
      'content-type': 'image/svg+xml',
      'cache-control': 'no-store'
    });
    response.end(svgImage());
    return;
  }

  if (url.pathname === adminMediaPreviewUrl) {
    if (!hasAdminAccess(request)) {
      sendJson(response, 404, { detail: 'Media file was not found.' });
      return;
    }
    response.writeHead(307, {
      location: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
      'cache-control': 'no-store'
    });
    response.end();
    return;
  }

  if (url.pathname === '/api/v1/auth/session') {
    if (hasAdminAccess(request)) {
      sendJson(response, 200, sessionPayload('admin'));
      return;
    }
    const token = accessToken(request);
    // Reproduce a lagging layout refresh once; the client store must retain the session already confirmed by polling.
    if (token?.startsWith('modal-full-') && staleFullSessionReads.delete(token)) {
      sendJson(response, 200, sessionPayload('guest'));
      return;
    }
    if (hasFullAccess(request)) {
      sendJson(response, 200, sessionPayload('full'));
      return;
    }
    const loginAttempt = cookieValue(request, 'smoke_login_attempt');
    if (loginAttempt && loginAttemptPolls.has(loginAttempt)) {
      const pollCount = (loginAttemptPolls.get(loginAttempt) ?? 0) + 1;
      loginAttemptPolls.set(loginAttempt, pollCount);
      if (pollCount >= 2) {
        const accessToken = `modal-full-${loginAttempt}`;
        staleFullSessionReads.add(accessToken);
        sendJson(response, 200, sessionPayload('full'), {
          'set-cookie': `memexpert_access_token=${accessToken}; Path=/; HttpOnly; SameSite=Lax`
        });
      } else {
        sendJson(response, 200, sessionPayload('guest'));
      }
      return;
    }

    sendJson(response, 401, { detail: 'Smoke test runs as a guest browser.' });
    return;
  }

  if (url.pathname === '/api/v1/auth/link/telegram' && request.method === 'POST') {
    const attempt = `modal-${++loginAttemptSequence}`;
    loginAttemptPolls.set(attempt, 0);
    sendJson(response, 200, {
      code: attempt,
      deep_link_url: `https://t.me/memexpertbot?start=link_${attempt}`,
      expires_at: '2099-12-31T23:59:59Z',
      expires_in_seconds: 600,
      return_url: '/account/telegram/complete'
    }, {
      'set-cookie': `smoke_login_attempt=${attempt}; Path=/; HttpOnly; SameSite=Lax`
    });
    return;
  }

  if (url.pathname === '/api/v1/auth/telegram-miniapp') {
    readRequestJson(request).then((body) => {
      if (!body || typeof body.initData !== 'string' || !body.initData.includes('smoke-miniapp-init-data')) {
        sendJson(response, 401, { detail: 'Invalid smoke Telegram initData.' });
        return;
      }

      sendJson(response, 200, sessionPayload('full'), {
        'set-cookie': 'memexpert_access_token=miniapp-full; Path=/; HttpOnly; SameSite=Lax'
      });
    }).catch(() => {
      sendJson(response, 400, { detail: 'Invalid JSON body.' });
    });
    return;
  }

  if (url.pathname.startsWith('/api/v1/admin/')) {
    const sessionKey = adminSessionKey(request);
    if (!sessionKey) {
      sendJson(response, 403, { detail: 'Admin access is required for smoke admin routes.' });
      return;
    }
    void handleAdminApi(request, response, url, adminSourcesForSession(sessionKey)).then((handled) => {
      if (!handled) sendJson(response, 404, { detail: `Unhandled smoke API route: ${url.pathname}` });
    }).catch(() => {
      sendJson(response, 500, { detail: 'Smoke admin API could not process the request.' });
    });
    return;
  }

  if (url.pathname === '/api/v1/collections') {
    if (!hasFullAccess(request)) {
      sendJson(response, 401, { detail: 'Sign in to load collection choices.' });
      return;
    }

    sendJson(response, 200, {
      collections: [seededCollectionSummary],
      active_save_collection_id: seededCollectionId
    });
    return;
  }

  if (url.pathname === '/api/v1/memes/meme-of-the-day') {
    sendJson(response, 200, {
      meme,
      selected_for: '2026-01-01',
      refreshed_at: '2026-01-01T00:00:00Z',
      algorithm_version: motdAlgorithmVersion,
      score: motdScore,
      score_components: motdScoreComponents,
      reason: 'Smoke MOTD selection',
      candidate_count: 2,
      attribution: motdAttribution()
    });
    return;
  }

  if (url.pathname === '/api/v1/memes/search') {
    if (isSeededCollectionSearch(url)) {
      if (!hasFullAccess(request)) {
        sendJson(response, 403, { detail: 'Sign in with access to this collection to search it.' });
        return;
      }

      sendJson(response, 200, searchPage([{ meme: collectionMeme, attribution: attributionFor(url, collectionMeme.id) }], url, 'req_smoke_collection'));
      return;
    }

    sendJson(response, 200, {
      items: [{ meme }],
      limit: Number(url.searchParams.get('limit') ?? 12),
      offset: Number(url.searchParams.get('offset') ?? 0),
      total: 1,
      has_more: false
    });
    return;
  }

  if (url.pathname === '/api/v1/memes/home-feed' || url.pathname === '/api/v1/memes/browse') {
    const offset = Number(url.searchParams.get('offset') ?? 0);
    sendJson(response, 200, {
      items: [{ meme: offset > 0 ? nextMeme : meme }],
      limit: Number(url.searchParams.get('limit') ?? 12),
      offset,
      total: 2,
      has_more: offset <= 0
    });
    return;
  }

  if (url.pathname === '/api/v1/memes/slug/smoke-test-cat-reaction') {
    sendJson(response, 200, detail);
    return;
  }

  if (url.pathname === '/api/v1/memes/smoke-meme-1/popularity') {
    sendJson(response, 200, {
      meme_id: 'smoke-meme-1',
      trend,
      sparkline: [
        { captured_at: '2026-01-01T00:00:00Z', source_views: 90, source_reactions: 4, source_reposts: 5, platform_views: 90, platform_sends: 5, platform_saves: 2, platform_likes: 4, popularity_score: 30 },
        { captured_at: '2026-01-01T01:00:00Z', source_views: 120, source_reactions: 7, source_reposts: 8, platform_views: 120, platform_sends: 8, platform_saves: 4, platform_likes: 7, popularity_score: 42.5 }
      ]
    });
    return;
  }

  if (/^\/api\/v1\/memes\/[^/]+\/(?:detail-click|download|impression|share)$/.test(url.pathname)) {
    sendJson(response, 200, { ok: true });
    return;
  }

  if (/^\/api\/v1\/memes\/[^/]+\/(?:favorite|save|pin)$/.test(url.pathname)) {
    sendJson(response, 200, request.method === 'DELETE' ? { removed: true } : { ok: true });
    return;
  }

  sendJson(response, 404, { detail: `Unhandled smoke API route: ${url.pathname}` });
});

server.listen(port, '127.0.0.1', () => {
  process.stdout.write(`Smoke API listening on http://127.0.0.1:${port}\n`);
});

async function handleAdminApi(request, response, url, adminSources) {
  const { method } = request;
  const { pathname } = url;

  if (method === 'GET' && pathname === '/api/v1/admin/session') {
    sendJson(response, 200, { user: sessionPayload('admin').user });
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/overview') {
    sendJson(response, 200, adminOverview);
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/channel-suggestions') {
    sendJson(response, 200, adminSuggestions);
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/source-channels') {
    sendJson(response, 200, adminSources);
    return true;
  }
  const sourcePostsMatch = method === 'GET'
    ? pathname.match(/^\/api\/v1\/admin\/source-channels\/([^/]+)\/posts$/)
    : null;
  if (sourcePostsMatch) {
    const source = adminSources.find((candidate) => candidate.id === sourcePostsMatch[1]);
    if (!source) {
      sendJson(response, 404, { detail: 'Smoke source was not found.' });
      return true;
    }
    const allItems = source.id === adminIds.healthySource ? adminSourcePosts : [];
    const limit = Math.max(1, Math.min(200, Number(url.searchParams.get('limit') ?? 50)));
    const offset = Math.max(0, Number(url.searchParams.get('offset') ?? 0));
    sendJson(response, 200, {
      source_channel_id: source.id,
      snapshot_at: url.searchParams.get('snapshot_at') ?? new Date().toISOString(),
      summary: sourcePostSummary(allItems),
      items: allItems.slice(offset, offset + limit),
      total: allItems.length,
      limit,
      offset
    });
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/telegram/sessions') {
    sendJson(response, 200, adminTelegramAccounts);
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/telegram/channels') {
    sendJson(response, 200, adminSources);
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/telegram/channels/grouped') {
    sendJson(response, 200, [
      {
        telegram_session: readyAdminTelegramAccount,
        is_orphaned: false,
        channels: adminSources.filter((source) => source.telegram_session_id === readyAdminTelegramAccount.id)
      },
      { telegram_session: floodWaitAdminTelegramAccount, is_orphaned: false, channels: [] },
      {
        telegram_session: null,
        is_orphaned: true,
        channels: adminSources.filter((source) => source.telegram_session_id === null)
      }
    ]);
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/moderation-reports') {
    sendJson(response, 200, [adminReport]);
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/moderation-decisions') {
    sendJson(response, 200, [adminDecision]);
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/memes') {
    sendJson(response, 200, [adminMeme]);
    return true;
  }
  if (method === 'GET' && pathname === `/api/v1/admin/memes/${adminMeme.id}`) {
    sendJson(response, 200, { meme: adminMeme, reports: [adminReport], decisions: [adminDecision] });
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/blocked-perceptual-hashes') {
    sendJson(response, 200, [adminBlockedPattern]);
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/seo-pages') {
    sendJson(response, 200, [{ meme: adminMeme, seo_page: adminSeoPage, status: 'generated' }]);
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/meme-templates') {
    sendJson(response, 200, adminTemplates);
    return true;
  }

  if (method === 'POST' && pathname === '/api/v1/admin/telegram/channels/from-reference') {
    const quickAdd = validateQuickAddRequest(await readRequestJson(request));
    if ('error' in quickAdd) {
      sendJson(response, 422, { detail: quickAdd.error });
      return true;
    }
    const source = upsertQuickAddedSource(adminSources, quickAdd.username);
    sendJson(response, 201, source);
    return true;
  }

  const backfillMatch = method === 'POST'
    ? pathname.match(/^\/api\/v1\/admin\/source-channels\/([^/]+)\/backfill$/)
    : null;
  if (backfillMatch) {
    const source = adminSources.find((candidate) => candidate.id === backfillMatch[1]);
    if (!source) {
      sendJson(response, 404, { detail: 'Smoke source was not found.' });
      return true;
    }
    const body = await readRequestJson(request);
    const messageLimit = body && typeof body === 'object' && !Array.isArray(body) ? body.message_limit : null;
    if (!Number.isInteger(messageLimit) || messageLimit < 1 || messageLimit > 50_000) {
      sendJson(response, 422, { detail: 'message_limit must be between 1 and 50000.' });
      return true;
    }
    Object.assign(source, {
      backfill_status: 'queued',
      backfill_requested_count: messageLimit,
      backfill_scanned_count: 0,
      backfill_error: null,
      history_exhausted: false,
      updated_at: new Date().toISOString()
    });
    sendJson(response, 202, source);
    return true;
  }

  const pauseMatch = method === 'POST'
    ? pathname.match(/^\/api\/v1\/admin\/source-channels\/([^/]+)\/pause$/)
    : null;
  if (pauseMatch) {
    const source = adminSources.find((candidate) => candidate.id === pauseMatch[1]);
    if (!source) {
      sendJson(response, 404, { detail: 'Smoke source was not found.' });
      return true;
    }
    source.is_paused = true;
    source.is_indexable = false;
    source.operational_status = 'paused';
    source.updated_at = new Date().toISOString();
    sendJson(response, 200, source);
    return true;
  }

  return false;
}

function validateQuickAddRequest(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return { error: 'Quick add requires a JSON object.' };
  }
  if (body.telegram_session_id !== readyAdminTelegramAccount.id) {
    return { error: 'Quick add must select the ready Telegram account.' };
  }
  if (body.catchup_message_limit !== 5000) {
    return { error: 'Quick add must use the default catch-up limit.' };
  }
  if (body.suggestion_id !== null && body.suggestion_id !== undefined && body.suggestion_id !== adminIds.telegramSuggestion) {
    return { error: 'Quick add received an unknown source suggestion.' };
  }
  const username = canonicalPublicTelegramUsername(body.reference);
  return username ? { username } : { error: 'Quick add requires one valid public Telegram reference.' };
}

function canonicalPublicTelegramUsername(reference) {
  if (typeof reference !== 'string') return null;
  const value = reference.trim();
  const handle = value.startsWith('@') ? value.slice(1) : value;
  if (/^[A-Za-z0-9_]{5,32}$/.test(handle)) return handle.toLowerCase();
  try {
    const url = new URL(value.startsWith('http') ? value : `https://${value}`);
    if (!['t.me', 'telegram.me'].includes(url.hostname.toLowerCase()) || url.search || url.hash) return null;
    const parts = url.pathname.split('/').filter(Boolean);
    return parts.length === 1 && /^[A-Za-z0-9_]{5,32}$/.test(parts[0]) ? parts[0].toLowerCase() : null;
  } catch {
    return null;
  }
}

function upsertQuickAddedSource(adminSources, username) {
  const now = new Date().toISOString();
  const existing = adminSources.find((source) => source.platform === 'telegram' && source.platform_id === username);
  const source = existing ?? {
    id: adminIds.quickAddedSource,
    platform: 'telegram',
    platform_id: username,
    username,
    title: username.split('_').map((part) => `${part[0].toUpperCase()}${part.slice(1)}`).join(' '),
    subscriber_count: null,
    is_active: true,
    is_paused: false,
    catchup_enabled: true,
    live_enabled: true,
    engagement_enabled: true,
    catchup_message_limit: 5000,
    telegram_session_id: readyAdminTelegramAccount.id,
    telegram_session_name: readyAdminTelegramAccount.name,
    is_orphaned: false,
    is_indexable: true,
    last_read_post_id: null,
    oldest_observed_post_id: null,
    initial_catchup_completed: false,
    history_exhausted: false,
    backfill_status: 'idle',
    backfill_requested_count: 0,
    backfill_scanned_count: 0,
    backfill_error: null,
    last_fetched_at: null,
    operational_status: 'active',
    freshness_status: 'never_fetched',
    seconds_since_last_fetch: null,
    created_at: now,
    updated_at: now
  };
  if (!existing) adminSources.push(source);

  Object.assign(source, {
    platform_id: username,
    username,
    is_active: true,
    is_paused: false,
    catchup_enabled: true,
    live_enabled: true,
    engagement_enabled: true,
    catchup_message_limit: 5000,
    telegram_session_id: readyAdminTelegramAccount.id,
    telegram_session_name: readyAdminTelegramAccount.name,
    is_orphaned: false,
    is_indexable: true,
    operational_status: 'active',
    updated_at: now
  });
  return source;
}

function sourcePostSummary(items) {
  return {
    observed_count: items.length,
    indexed_count: items.filter((item) => item.index_status === 'indexed').length,
    partially_indexed_count: items.filter((item) => item.index_status === 'partially_indexed').length,
    processing_count: items.filter((item) => item.index_status === 'processing').length,
    failed_count: items.filter((item) => item.index_status === 'failed').length,
    not_indexable_count: items.filter((item) => item.index_status === 'not_indexable').length
  };
}

function sendJson(response, status, payload, headers = {}) {
  response.writeHead(status, {
    'content-type': 'application/json',
    'cache-control': 'no-store',
    ...headers
  });
  response.end(JSON.stringify(payload));
}

function readRequestJson(request) {
  return new Promise((resolve, reject) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => {
      body += chunk;
    });
    request.on('end', () => {
      try {
        resolve(body ? JSON.parse(body) : null);
      } catch (error) {
        reject(error);
      }
    });
    request.on('error', reject);
  });
}

function hasFullAccess(request) {
  const token = accessToken(request);
  return token === 'miniapp-full' || token?.startsWith('modal-full-') || hasAdminAccess(request);
}

function hasAdminAccess(request) {
  return adminSessionKey(request) !== null;
}

function adminSessionKey(request) {
  const token = accessToken(request);
  if (!token?.startsWith(adminAccessTokenPrefix)) return null;

  const key = token.slice(adminAccessTokenPrefix.length);
  return key && /^[A-Za-z0-9_-]+$/.test(key) ? key : null;
}

function accessToken(request) {
  return cookieValue(request, 'memexpert_access_token');
}

function cookieValue(request, name) {
  for (const pair of (request.headers.cookie ?? '').split(';')) {
    const separator = pair.indexOf('=');
    if (separator < 0 || pair.slice(0, separator).trim() !== name) continue;
    return pair.slice(separator + 1).trim();
  }
  return null;
}

function adminSourcesForSession(sessionKey) {
  let sources = adminSourceStateBySession.get(sessionKey);
  if (!sources) {
    sources = structuredClone(adminSourceSeed);
    adminSourceStateBySession.set(sessionKey, sources);
  }
  return sources;
}

function isSeededCollectionSearch(url) {
  return url.searchParams.get('scope') === 'collections' && url.searchParams.getAll('collection_ids').includes(seededCollectionId);
}

function searchPage(items, url, requestId) {
  return {
    items,
    limit: Number(url.searchParams.get('limit') ?? 12),
    offset: Number(url.searchParams.get('offset') ?? 0),
    total: items.length,
    has_more: false,
    request_id: requestId
  };
}

function attributionFor(url, memeId) {
  return {
    request_id: 'req_smoke_collection',
    impression_id: `imp_${memeId}`,
    surface: 'search',
    source_algorithm: 'smoke-collection-seed',
    rank: 1,
    query: url.searchParams.get('query') ?? null,
    filters: {
      language: url.searchParams.get('language'),
      media_type: url.searchParams.get('media_type'),
      include_nsfw: url.searchParams.get('include_nsfw') === 'true',
      tags: url.searchParams.getAll('tags'),
      scope: url.searchParams.get('scope'),
      collection_ids: url.searchParams.getAll('collection_ids')
    },
    collection_scope: 'collections',
    collection_ids: url.searchParams.getAll('collection_ids'),
    source_meme_id: null,
    algorithm_version: 'smoke-v1',
    score: 1,
    score_components: { collection_match: 1 },
    reason: 'Seeded readable collection result'
  };
}

function motdAttribution() {
  return {
    request_id: 'req_smoke_motd',
    impression_id: 'imp_smoke_motd',
    surface: 'web_home',
    source_algorithm: 'motd',
    rank: 1,
    query: null,
    filters: {
      language: null,
      media_type: null,
      include_nsfw: false,
      tags: [],
      scope: null,
      collection_ids: []
    },
    collection_scope: null,
    collection_ids: [],
    source_meme_id: null,
    algorithm_version: motdAlgorithmVersion,
    score: motdScore,
    score_components: motdScoreComponents,
    reason: 'Smoke MOTD selection'
  };
}

function sessionPayload(accountType) {
  const isAdmin = accountType === 'admin';
  const isFull = accountType === 'full' || isAdmin;
  return {
    user: {
      id: isAdmin ? adminIds.user : isFull ? 'smoke-full-user' : 'smoke-guest-user',
      account_type: isFull ? 'full' : 'guest',
      telegram_id: isFull ? 303030303 : null,
      google_id: null,
      email: isAdmin ? 'admin@smoke.test' : null,
      email_verified_at: null,
      language: 'any',
      nsfw_enabled: false,
      token_nonce: 0,
      status: 'active',
      guest_expires_at: isFull ? null : '2099-12-31T23:59:59Z',
      active_save_collection_id: null,
      is_admin: isAdmin,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z'
    },
    linked_providers: {
      email: null,
      email_verified_at: null,
      has_password: false,
      google_linked: false,
      telegram_linked: isFull
    }
  };
}

function svgImage() {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360" role="img" aria-label="Smoke test cat reaction">
  <rect width="640" height="360" fill="#111827"/>
  <circle cx="220" cy="165" r="82" fill="#facc15"/>
  <circle cx="190" cy="145" r="12" fill="#111827"/>
  <circle cx="250" cy="145" r="12" fill="#111827"/>
  <path d="M180 220 Q220 250 260 220" fill="none" stroke="#111827" stroke-width="14" stroke-linecap="round"/>
  <text x="340" y="150" fill="#f9fafb" font-family="Arial, sans-serif" font-size="34" font-weight="700">ship it</text>
  <text x="340" y="200" fill="#93c5fd" font-family="Arial, sans-serif" font-size="24">smoke media</text>
</svg>`;
}
