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
    id: 'smoke-file-2',
    width: 4096,
    height: 1024,
    render: {
      ...meme.primary_file.render,
      width: 4096,
      height: 1024
    }
  }
};

const similarMemes = Array.from({ length: 25 }, (_, index) => {
  const rank = index + 1;
  return {
    ...nextMeme,
    id: `smoke-similar-${rank}`,
    caption: `Smoke similar meme ${rank}`,
    seo_page_slug: `smoke-similar-meme-${rank}`,
    primary_file: {
      ...nextMeme.primary_file,
      id: `smoke-similar-file-${rank}`
    }
  };
});

const videoMeme = {
  ...meme,
  id: 'smoke-video-1',
  media_type: 'video',
  caption: null,
  seo_page_slug: null,
  tags: [],
  primary_file: {
    ...meme.primary_file,
    id: 'smoke-video-file-1',
    mime_type: 'video/mp4',
    width: 720,
    height: 1280,
    render: {
      ...meme.primary_file.render,
      display_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
      original_url: `http://127.0.0.1:${port}/media/smoke-video.mp4`,
      download_url: `http://127.0.0.1:${port}/media/smoke-video.mp4`,
      web_video_url: `http://127.0.0.1:${port}/media/smoke-video.mp4`,
      width: 720,
      height: 1280
    },
    render_url: `http://127.0.0.1:${port}/media/smoke-video.mp4`,
    download_url: `http://127.0.0.1:${port}/media/smoke-video.mp4`
  },
  render_url: `http://127.0.0.1:${port}/media/smoke-video.mp4`,
  download_url: `http://127.0.0.1:${port}/media/smoke-video.mp4`
};

const rankedMasonryQuery = 'ranked masonry smoke';
const rankedMasonryMemes = [
  rankedMasonryMeme(1, 'Ranked portrait one', 480, 960),
  rankedMasonryMeme(2, 'Ranked ultra wide two', 1600, 400),
  rankedMasonryMeme(3, 'Ranked square three', 800, 800),
  rankedMasonryMeme(4, 'Ranked landscape four', 1200, 675),
  rankedMasonryMeme(5, null, 900, 600, { tags: ['Ranked captionless five'] }),
  rankedMasonryMeme(6, 'Ranked missing media six', 640, 360, { missingMedia: true }),
  rankedMasonryMeme(7, 'Ranked video seven', 720, 1280, { mediaType: 'video' }),
  rankedMasonryMeme(8, 'Ranked tall eight with enough caption copy to make its card height distinct', 600, 1200),
  rankedMasonryMeme(9, 'Ranked appended portrait nine', 500, 1000),
  rankedMasonryMeme(10, 'Ranked appended wide ten', 1800, 450)
];

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

const favoritesCollectionId = 'smoke-favorites';
const recentCollectionId = 'smoke-recent-reactions';
const laterCollectionId = 'smoke-later-ideas';
const writableCollectionCapabilities = {
  can_view: true,
  can_add_memes: true,
  can_remove_memes: true,
  can_rename: true,
  can_delete: true,
  can_create_invites: true,
  can_revoke_invites: true,
  can_manage_members: true,
  can_set_active_save: true
};
const favoritesCollectionSummary = {
  collection: {
    ...seededCollection,
    id: favoritesCollectionId,
    owner_id: 'smoke-full-user',
    title: 'Favorites',
    description: null,
    kind: 'favorites'
  },
  viewer_role: 'owner',
  capabilities: writableCollectionCapabilities,
  active_save_collection_id: favoritesCollectionId
};
const recentCollectionSummary = {
  collection: {
    ...seededCollection,
    id: recentCollectionId,
    owner_id: 'smoke-full-user',
    title: 'Recent reactions',
    description: 'Most recently used writable collection.'
  },
  viewer_role: 'owner',
  capabilities: writableCollectionCapabilities,
  active_save_collection_id: favoritesCollectionId
};
const laterCollectionSummary = {
  collection: {
    ...seededCollection,
    id: laterCollectionId,
    owner_id: 'smoke-full-user',
    title: 'Later ideas',
    description: 'Another writable collection.'
  },
  viewer_role: 'owner',
  capabilities: writableCollectionCapabilities,
  active_save_collection_id: favoritesCollectionId
};

const viewerMemeStates = new Map();
let guestStateSequence = 0;

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

const smokeSourceCoverage = {
  views: { measured_posts: 1, total_posts: 1, ratio: 1 },
  reactions: { measured_posts: 1, total_posts: 1, ratio: 1 },
  comments: { measured_posts: 0, total_posts: 1, ratio: 0 },
  reposts: { measured_posts: 1, total_posts: 1, ratio: 1 }
};
const smokeSourceRates = {
  reactions: { value: 0.05, numerator: 6, denominator: 120, eligible_posts: 1, total_posts: 1 },
  comments: { value: null, numerator: null, denominator: null, eligible_posts: 0, total_posts: 1 },
  reposts: { value: 0.025, numerator: 3, denominator: 120, eligible_posts: 1, total_posts: 1 },
  interactions: { value: null, numerator: null, denominator: null, eligible_posts: 0, total_posts: 1 }
};
const smokeSourceSummary = {
  total_posts: 1,
  available_posts: 1,
  distinct_channels: 1,
  earliest_published_at: '2025-12-31T18:00:00Z',
  latest_published_at: '2025-12-31T18:00:00Z',
  latest_captured_at: '2026-01-02T00:00:00Z',
  totals: { views: 120, reactions: 6, comments: null, reposts: 3 },
  coverage: smokeSourceCoverage,
  rates: smokeSourceRates,
  audience: {
    current_known_channels: 1,
    total_channels: 1,
    publish_time_eligible_posts: 1,
    total_posts: 1,
    views_per_1000_subscribers: { value: 240, numerator: 120, denominator: 500, eligible_posts: 1, total_posts: 1 },
    interactions_per_1000_subscribers: { value: null, numerator: null, denominator: null, eligible_posts: 0, total_posts: 1 }
  }
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

const adminAnalyticsRange = {
  start_date: '2026-06-01',
  end_date: '2026-06-30',
  comparison_start_date: '2026-05-02',
  comparison_end_date: '2026-05-31',
  timezone: 'UTC',
  bucket: 'day'
};

function analyticsMetric(value, previousValue = Math.max(0, value - 2)) {
  const change = value - previousValue;
  return {
    value,
    previous_value: previousValue,
    change,
    change_percent: previousValue ? Number(((change / previousValue) * 100).toFixed(1)) : null
  };
}

const adminAnalyticsQuery = {
  query_key: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
  query: 'frog reaction',
  searches: 9,
  zero_result_searches: 1,
  zero_result_rate: 11.1,
  average_latency_ms: 44,
  detail_clicks: 3,
  downloads: 2
};

const adminAnalyticsOverview = {
  range: adminAnalyticsRange,
  metrics: {
    catalog_memes: analyticsMetric(512), new_memes: analyticsMetric(24), page_views: analyticsMetric(146), active_users: analyticsMetric(48), interactions: analyticsMetric(109), downloads: analyticsMetric(13), guest_to_full_conversions: analyticsMetric(4)
  },
  activity: [
    { date: '2026-06-01', page_views: 42, active_users: 15, interactions: 31, searches: 18, downloads: 3, new_memes: 8 },
    { date: '2026-06-02', page_views: 56, active_users: 19, interactions: 43, searches: 24, downloads: 5, new_memes: 10 }
  ],
  discovery_funnel: { searches: 42, searches_with_results: 36, searches_without_results: 6, detail_clicks: 18, downloads: 8 },
  surface_mix: [{ surface: 'web_search', count: 63 }, { surface: 'telegram_inline', count: 31 }],
  source_activity: { sources: 7, new_sources: 2, source_views: 210, source_reactions: 23, source_reposts: 8 }
};

const adminAnalyticsEngagement = {
  range: adminAnalyticsRange,
  metrics: {
    interactions: analyticsMetric(109), searches: analyticsMetric(42), zero_result_searches: analyticsMetric(6), zero_result_rate: analyticsMetric(14.3), average_search_latency_ms: analyticsMetric(44), detail_clicks: analyticsMetric(18), downloads: analyticsMetric(8), sends: analyticsMetric(12), saves: analyticsMetric(9), shares: analyticsMetric(4)
  },
  activity: [
    { date: '2026-06-01', interactions: 31, searches: 18, zero_result_searches: 2, detail_clicks: 7, downloads: 3, sends: 4, saves: 3, shares: 1 },
    { date: '2026-06-02', interactions: 43, searches: 24, zero_result_searches: 4, detail_clicks: 11, downloads: 5, sends: 6, saves: 4, shares: 2 }
  ],
  interactions_by_type: [{ key: 'meme_download', count: 8 }, { key: 'meme_save', count: 6 }, { key: 'meme_share', count: 4 }],
  surface_mix: [{ surface: 'web_search', count: 63 }, { surface: 'telegram_inline', count: 31 }],
  top_search_queries: [adminAnalyticsQuery]
};

const adminAnalyticsAudience = {
  range: adminAnalyticsRange,
  metrics: {
    new_guests: analyticsMetric(20), new_full_accounts: analyticsMetric(8), active_users: analyticsMetric(48), active_guests: analyticsMetric(29), active_full_accounts: analyticsMetric(19), guest_to_full_conversions: analyticsMetric(4), guest_to_full_conversion_rate: analyticsMetric(20)
  },
  activity: [
    { date: '2026-06-01', new_guests: 9, new_full_accounts: 3, active_users: 15, guest_to_full_conversions: 2 },
    { date: '2026-06-02', new_guests: 11, new_full_accounts: 5, active_users: 19, guest_to_full_conversions: 2 }
  ],
  surface_mix: [{ surface: 'web_search', count: 35 }, { surface: 'telegram_inline', count: 13 }],
  retention_cohorts: [{ cohort_date: '2026-05-01', cohort_size: 10, d1: { eligible_users: 10, retained_users: 5, rate: 50 }, d7: { eligible_users: 10, retained_users: 3, rate: 30 }, d30: { eligible_users: 10, retained_users: 2, rate: 20 } }]
};

const adminAnalyticsContent = {
  range: adminAnalyticsRange,
  metrics: {
    catalog_memes: analyticsMetric(512), new_memes: analyticsMetric(24), public_memes: analyticsMetric(460), private_memes: analyticsMetric(52), nsfw_memes: analyticsMetric(18), seo_pages: analyticsMetric(401), active_sources: analyticsMetric(7), new_sources: analyticsMetric(2), source_views: analyticsMetric(210), source_reactions: analyticsMetric(23), source_reposts: analyticsMetric(8)
  },
  catalog_growth: [{ date: '2026-06-01', new_memes: 8 }, { date: '2026-06-02', new_memes: 10 }],
  media_types: [{ key: 'image', count: 420 }, { key: 'gif', count: 60 }],
  languages: [{ key: 'en', count: 310 }, { key: 'ru', count: 140 }],
  visibility: [{ key: 'public', count: 460 }, { key: 'private', count: 52 }],
  processing: [{ key: 'ready', count: 500 }, { key: 'processing', count: 12 }],
  source_health: [{ key: 'fresh', count: 5 }, { key: 'stale', count: 2 }],
  source_engagement: [{ date: '2026-06-01', source_views: 92, source_reactions: 9, source_reposts: 3 }, { date: '2026-06-02', source_views: 118, source_reactions: 14, source_reposts: 5 }]
};

const adminRecoveryWork = [
  recoveryWork({
    kind: 'backfill',
    id: 'smoke-backfill-memach',
    title: '@memach backfill',
    source_label: '@memach',
    source_channel_id: adminIds.staleSource,
    version: 'backfill-version-1',
    capabilities: ['resume_backfill']
  }),
  recoveryWork({
    kind: 'backfill',
    id: 'smoke-backfill-log4inpowerken',
    title: '@log4inpowerken backfill',
    source_label: '@log4inpowerken',
    source_channel_id: adminIds.healthySource,
    version: 'backfill-version-2',
    capabilities: ['resume_backfill']
  }),
  recoveryWork({
    kind: 'pipeline_stage',
    id: 'smoke-file-ocr-1:ocr',
    title: 'OCR file one',
    meme_file_id: 'smoke-file-ocr-1',
    stage: 'ocr',
    error_code: 'ocr_timeout',
    reason: 'ocr_timeout',
    safe_error: 'OCR exceeded its processing deadline.',
    version: 'ocr-version-1',
    capabilities: ['retry_stage'],
    actions: [
      { capability: 'retry_stage', available: true, scopes: ['stage_only'] },
      {
        capability: 'replay_stage',
        available: true,
        scopes: ['stage_only', 'stage_and_dependents'],
        scope_requirements: {
          stage_only: { warnings: [], risks: [], required_acknowledgements: [] },
          stage_and_dependents: {
            warnings: [],
            risks: ['External provider output may differ from the previous run.'],
            required_acknowledgements: ['terminal_override']
          }
        }
      }
    ]
  }),
  recoveryWork({
    kind: 'pipeline_stage',
    id: 'smoke-file-ocr-2:ocr',
    title: 'OCR file two',
    meme_file_id: 'smoke-file-ocr-2',
    stage: 'ocr',
    error_code: 'ocr_timeout',
    reason: 'ocr_timeout',
    safe_error: 'OCR exceeded its processing deadline.',
    version: 'ocr-version-2',
    capabilities: ['retry_stage']
  }),
  recoveryWork({
    kind: 'source_post',
    id: 'smoke-blocked-post',
    bucket: 'blocked',
    title: 'Blocked Telegram post',
    source_label: '@offline_source',
    post_id: '404',
    status: 'failed',
    error_code: 'source_account_unavailable',
    reason: 'source_account_unavailable',
    safe_error: null,
    is_retryable: false,
    version: 'blocked-version-1',
    capabilities: [],
    actions: [
      {
        capability: 'replay_source_post',
        available: false,
        blocked_prerequisites: ['Reconnect the source account before retrying.']
      }
    ],
    blocked_reason: 'Reconnect the source account before retrying.'
  })
];

const adminRecoverySummary = {
  retryable_count: 4,
  blocked_count: 1,
  stuck_count: 0,
  dead_lettered_count: 0,
  outdated_web_video_count: 7400,
  active_job_count: 1,
  preparing_job_count: 0,
  snapshot_at: '2026-07-15T12:00:00Z'
};

const adminRecoveryJob = {
  id: '77777777-7777-4777-8777-777777777777',
  request_id: '66666666-6666-4666-8666-666666666666',
  status: 'completed_with_failures',
  action: 'replay_stage',
  scope: 'stage_and_dependents',
  retry_limit: 3,
  reason: 'Smoke replay and repair job.',
  total_count: 6,
  selected_root_count: 2,
  expanded_execution_count: 6,
  completed_count: 6,
  failed_count: 1,
  queued_count: 0,
  waiting_count: 0,
  dispatched_count: 0,
  succeeded_count: 4,
  stale_count: 0,
  skipped_count: 1,
  cancelled_count: 0,
  exclusion_groups: [{ reason: 'changed_since_snapshot', count: 1, message: 'Canonical state changed.' }],
  requested_by_display_name: 'Smoke requester',
  assigned_to_display_name: 'Smoke operator',
  expires_at: null,
  scheduled_at: '2026-07-15T12:01:00Z',
  completed_at: '2026-07-15T12:03:00Z',
  cancelled_at: null,
  created_at: '2026-07-15T12:00:00Z',
  updated_at: '2026-07-15T12:03:00Z',
  version: 'job-version-1',
  items: []
};

const adminRecoveryJobItems = [
  {
    id: '88888888-8888-4888-8888-888888888888',
    work_kind: 'pipeline_stage',
    work_id: 'smoke-video-file-1:ocr',
    action: 'replay_stage',
    stage: 'ocr',
    status: 'failed',
    attempt_count: 3,
    retry_limit: 3,
    retryable_failures_consumed: 3,
    normalized_reason: 'ocr_timeout',
    safe_error: 'OCR exceeded its safe processing deadline.',
    dispatched_at: '2026-07-15T12:01:00Z',
    finished_at: '2026-07-15T12:03:00Z'
  },
  {
    id: '99999999-9999-4999-8999-999999999999',
    work_kind: 'pipeline_stage',
    work_id: 'smoke-video-file-1:transcode',
    action: 'replay_stage',
    stage: 'transcode',
    status: 'succeeded',
    attempt_count: 1,
    retry_limit: 3,
    retryable_failures_consumed: 0,
    normalized_reason: null,
    safe_error: null,
    dispatched_at: '2026-07-15T12:01:00Z',
    finished_at: '2026-07-15T12:02:00Z'
  }
];

const adminMeme = {
  ...meme,
  id: adminIds.meme,
  primary_file: adminMediaFile,
  is_public: false,
  visibility_mode: 'force_private',
  template_id: adminIds.curatedTemplate,
};

const adminMemeProcessingFiles = [
  {
    id: adminIds.mediaFile,
    is_primary: true,
    status: 'ready',
    mime_type: 'video/webm',
    width: 1280,
    height: 720,
    file_size_bytes: 4096,
    source_has_audio: true,
    web_video_has_audio: true,
    web_video_profile: 'web-h264-aac-1080p30-v2',
    web_video_verified_at: '2026-07-15T11:30:00Z',
    original: { width: 1280, height: 720, frame_rate: 24, duration_seconds: 4, file_size_bytes: 4096, video_codec: 'vp9', audio_codec: 'opus' },
    output: { width: 1280, height: 720, frame_rate: 24, duration_seconds: 4, bitrate_bps: 2_000_000, file_size_bytes: 2048, video_codec: 'h264', audio_codec: 'aac', pixel_format: 'yuv420p', video_profile: 'High' },
    version: 'smoke-processing-file-version',
    actions: [
      {
        capability: 'regenerate_derivatives',
        available: true,
        scopes: ['stage_only'],
        default_scope: 'stage_only',
        retry_limits: [1, 3, 5],
        default_retry_limit: 3,
        downstream_stages: [],
        warnings: [],
        risks: [],
        required_acknowledgements: [],
        blocked_prerequisites: []
      }
    ],
    stages: [
      {
        stage: 'transcode',
        status: 'succeeded',
        attempt_count: 1,
        version: 'smoke-transcode-version',
        work_kind: 'pipeline_stage',
        work_id: `${adminIds.mediaFile}:transcode`,
        safe_error: null,
        normalized_reason: null,
        actions: [
          {
            capability: 'replay_stage',
            available: true,
            scopes: ['stage_only', 'stage_and_dependents'],
            default_scope: 'stage_only',
            retry_limits: [1, 3, 5],
            default_retry_limit: 3,
            downstream_stages: ['ocr', 'embed', 'classify', 'sync_qdrant', 'sync_meili'],
            warnings: ['Stage-only replay leaves existing downstream data stale.'],
            risks: [],
            required_acknowledgements: [],
            scope_requirements: {
              stage_only: {
                warnings: ['Stage-only replay leaves existing downstream data stale.'],
                risks: [],
                required_acknowledgements: []
              },
              stage_and_dependents: {
                warnings: [],
                risks: ['External provider output or semantic merge results may differ from the previous successful run.'],
                required_acknowledgements: [{ key: 'terminal_override', label: 'I acknowledge the terminal override.' }]
              }
            },
            blocked_prerequisites: []
          }
        ],
        active_job: null
      }
    ],
    active_job: null
  }
];

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
    latest_post_at: '2026-01-01T00:00:00Z',
    observed_post_count: 25,
    meme_count: 18,
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
    latest_post_at: null,
    observed_post_count: 0,
    meme_count: 0,
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
    latest_post_at: '2025-12-25T00:00:00Z',
    observed_post_count: 18,
    meme_count: 11,
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
const transientQuickAddFailures = new WeakSet();
const transientSourceRefreshFailures = new WeakSet();
const loginAttemptPolls = new Map();
const staleFullSessionReads = new Set();
const failedSimilarRefreshTokens = new Set();
let loginAttemptSequence = 0;
let detailReadCount = 0;
let detailViewCount = 0;
let similarInitialReadCount = 0;
const rotatingHomeReadCounts = new Map();

const adminSourcePosts = [
  {
    id: 'source-post-indexed',
    post_id: '184',
    telegram_url: 'https://t.me/daily_cats/184',
    published_at: '2026-01-01T00:00:00Z',
    observed_at: '2026-01-01T00:00:10Z',
    media_type: 'image',
    metadata_state: 'captured',
    text_excerpt: 'Cats & coffee\nStay curious 🐈',
    media_group_id: '9007199254740993',
    reply_to_post_id: '179',
    telegram_edited_at: '2026-01-01T00:05:00Z',
    metadata_first_observed_at: '2026-01-01T00:00:10Z',
    metadata_last_observed_at: '2026-01-01T00:06:00Z',
    is_deleted: false,
    deletion_observed_at: null,
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
    metadata_state: 'captured',
    text_excerpt: null,
    media_group_id: '9007199254740993',
    reply_to_post_id: null,
    telegram_edited_at: null,
    metadata_first_observed_at: '2026-01-01T00:00:11Z',
    metadata_last_observed_at: '2026-01-01T00:00:11Z',
    is_deleted: false,
    deletion_observed_at: null,
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
    metadata_state: 'missing',
    text_excerpt: null,
    media_group_id: null,
    reply_to_post_id: null,
    telegram_edited_at: null,
    metadata_first_observed_at: null,
    metadata_last_observed_at: null,
    is_deleted: false,
    deletion_observed_at: null,
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
    metadata_state: 'captured',
    text_excerpt: 'This retained caption remains available after deletion.',
    media_group_id: null,
    reply_to_post_id: '180',
    telegram_edited_at: '2025-12-31T21:05:00Z',
    metadata_first_observed_at: '2026-01-01T00:00:13Z',
    metadata_last_observed_at: '2026-01-01T00:10:00Z',
    is_deleted: true,
    deletion_observed_at: '2026-01-01T00:15:00Z',
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
    metadata_state: 'captured',
    text_excerpt: 'Standalone text-only Telegram post.',
    media_group_id: null,
    reply_to_post_id: null,
    telegram_edited_at: null,
    metadata_first_observed_at: '2026-01-01T00:00:14Z',
    metadata_last_observed_at: '2026-01-01T00:00:14Z',
    is_deleted: false,
    deletion_observed_at: null,
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
  previous_visibility_mode: 'auto',
  previous_is_nsfw: true,
  new_is_public: true,
  new_visibility_mode: 'auto',
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

  if (url.pathname === '/__smoke/stats') {
    sendJson(response, 200, { detailReadCount, detailViewCount, similarInitialReadCount });
    return;
  }

  if (url.pathname === '/health') {
    sendJson(response, 200, { ok: true });
    return;
  }

  if (url.pathname === '/api/v1/seo/summary') {
    sendJson(response, 200, {
      public_safe_meme_count: 10,
      tag_count: 6,
      template_count: 2,
      updated_at: '2026-01-01T00:00:00Z'
    });
    return;
  }

  if (url.pathname === '/api/v1/analytics/interactions/batch' && request.method === 'POST') {
    readRequestJson(request).then((body) => {
      const count = Array.isArray(body?.events) ? body.events.length : 0;
      sendJson(response, 202, { recorded: count, duplicates: 0 });
    }).catch(() => sendJson(response, 400, { detail: 'Invalid interaction batch.' }));
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
    if (token === 'guest-auto' || token?.startsWith('smoke-guest-state-')) {
      sendJson(response, 200, sessionPayload('guest'));
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
    const fullAccess = hasFullAccess(request);
    const bootstrapHeaders = fullAccess || accessToken(request)
      ? {}
      : viewerState(request, { bootstrap: true }).headers;
    sendJson(response, 200, {
      collections: fullAccess
        ? [recentCollectionSummary, favoritesCollectionSummary, laterCollectionSummary, seededCollectionSummary]
        : [favoritesCollectionSummary],
      active_save_collection_id: favoritesCollectionId
    }, bootstrapHeaders);
    return;
  }

  const memeChoicesMatch = url.pathname.match(/^\/api\/v1\/collections\/meme-choices\/([^/]+)$/);
  if (memeChoicesMatch) {
    const state = viewerState(request).state;
    state.collectionChoiceReadCount += 1;
    const choices = hasFullAccess(request)
      ? [
          collectionChoice(recentCollectionSummary, state.savedCollectionIds.has(recentCollectionId)),
          collectionChoice(laterCollectionSummary, state.savedCollectionIds.has(laterCollectionId))
        ]
      : [];
    const payload = {
      collections: choices.sort((left, right) => Number(right.contains_meme) - Number(left.contains_meme))
    };
    if (accessToken(request) === 'smoke-full-save-race' && state.collectionChoiceReadCount === 2) {
      setTimeout(() => sendJson(response, 200, payload), 2_000);
      return;
    }
    sendJson(response, 200, payload);
    return;
  }

  if (url.pathname === '/api/v1/memes/meme-of-the-day') {
    sendJson(response, 200, {
      meme: projectViewerMeme(request, meme),
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

    if ((url.searchParams.get('query') ?? '').trim().toLowerCase() === 'video') {
      sendJson(response, 200, searchPage([{ meme: videoMeme, attribution: attributionFor(url, videoMeme.id) }], url, 'req_smoke_video'));
      return;
    }

    if ((url.searchParams.get('query') ?? '').trim().toLowerCase() === rankedMasonryQuery) {
      const offset = Math.max(0, Number(url.searchParams.get('offset') ?? 0));
      const limit = 8;
      const pageMemes = rankedMasonryMemes.slice(offset, offset + limit);
      sendJson(response, 200, {
        items: pageMemes.map((item, index) => ({
          meme: projectViewerMeme(request, item),
          attribution: rankedMasonryAttributionFor(url, item.id, offset + index + 1)
        })),
        limit,
        offset,
        total: rankedMasonryMemes.length,
        has_more: offset + pageMemes.length < rankedMasonryMemes.length,
        request_id: 'req_smoke_ranked_masonry'
      });
      return;
    }

    sendJson(response, 200, {
      items: [{ meme: projectViewerMeme(request, meme) }],
      limit: Number(url.searchParams.get('limit') ?? 12),
      offset: Number(url.searchParams.get('offset') ?? 0),
      total: 1,
      has_more: false
    });
    return;
  }

  if (url.pathname === '/api/v1/memes/home-feed/reauthorize' && request.method === 'POST') {
    readRequestJson(request).then((body) => {
      const knownMemes = new Map([meme, nextMeme].map((item) => [item.id, item]));
      const items = Array.isArray(body?.items)
        ? body.items.flatMap((item, index) => {
            const restoredMeme = knownMemes.get(item?.meme_id);
            return restoredMeme
              ? [{
                  meme: projectViewerMeme(request, restoredMeme),
                  attribution: homeAttribution(restoredMeme.id, index + 1, item.attribution_token)
                }]
              : [];
          })
        : [];
      sendJson(response, 200, { items });
    }).catch(() => sendJson(response, 400, { detail: 'Invalid saved Home feed.' }));
    return;
  }

  if (url.pathname === '/api/v1/memes/home-feed') {
    const offset = url.searchParams.has('cursor') ? 12 : Number(url.searchParams.get('offset') ?? 0);
    const token = accessToken(request);
    let rotatingReadCount = 0;
    if (offset <= 0 && token === 'smoke-full-home-refresh') {
      rotatingReadCount = (rotatingHomeReadCounts.get(token) ?? 0) + 1;
      rotatingHomeReadCounts.set(token, rotatingReadCount);
    }
    const homeMeme = offset > 0 || (rotatingReadCount > 0 && rotatingReadCount % 2 === 0)
      ? nextMeme
      : meme;
    const bootstrapHeaders = accessToken(request)
      ? {}
      : viewerState(request, { bootstrap: true }).headers;
    sendJson(response, 200, {
      items: [{
        meme: projectViewerMeme(request, homeMeme),
        attribution: homeAttribution(homeMeme.id, offset + 1)
      }],
      limit: Number(url.searchParams.get('limit') ?? 12),
      offset,
      total: 2,
      has_more: offset <= 0,
      request_id: `req_smoke_home_${offset}_${rotatingReadCount}`,
      feed_session_id: rotatingReadCount > 0 ? `feed_smoke_home_${rotatingReadCount}` : 'feed_smoke_home',
      next_cursor: offset <= 0 ? 'smoke-signed-home-cursor' : null,
      expires_at: '2099-01-01T00:00:00Z'
    }, bootstrapHeaders);
    return;
  }

  if (url.pathname === '/api/v1/memes/browse') {
    const offset = Number(url.searchParams.get('offset') ?? 0);
    sendJson(response, 200, {
      items: [{ meme: projectViewerMeme(request, offset > 0 ? nextMeme : meme) }],
      limit: Number(url.searchParams.get('limit') ?? 12),
      offset,
      total: 2,
      has_more: offset <= 0,
      request_id: `req_smoke_browse_${offset}`
    });
    return;
  }

  if (url.pathname === '/api/v1/memes/slug/smoke-test-cat-reaction') {
    detailReadCount += 1;
    sendJson(response, 200, { ...detail, ...projectViewerMeme(request, meme) });
    return;
  }

  if (url.pathname === '/api/v1/memes/slug/smoke-similar-meme-1') {
    const candidate = similarMemes[0];
    sendJson(response, 200, {
      ...detail,
      ...projectViewerMeme(request, candidate),
      ocr_text: 'similar source one',
      seo_title: candidate.caption,
      seo_description: 'A second deterministic source used to verify Similar feed resets.',
      seo_body_text: 'Navigating here must discard pending pages from the previous source meme.',
      files: [candidate.primary_file]
    });
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

  if (url.pathname === '/api/v1/memes/smoke-meme-1/sources') {
    sendJson(response, 200, {
      meme_id: 'smoke-meme-1',
      snapshot_at: url.searchParams.get('snapshot_at') ?? '2026-01-02T00:00:00Z',
      sort: url.searchParams.get('sort') ?? 'views_desc',
      items: [{
        channel_title: 'Smoke Memes Lab',
        channel_username: 'smoke_memes_lab',
        channel_url: 'https://t.me/smoke_memes_lab',
        post_url: 'https://t.me/smoke_memes_lab/42',
        published_at: '2025-12-31T18:00:00Z',
        available: true,
        captured_at: '2026-01-02T00:00:00Z',
        views: 120,
        reactions: 6,
        comments: null,
        reposts: 3,
        rates: smokeSourceRates,
        audience: {
          audience_at_publish: 500,
          current_audience: 540,
          views_per_1000_subscribers: 240,
          interactions_per_1000_subscribers: null
        }
      }],
      summary: smokeSourceSummary,
      limit: Number(url.searchParams.get('limit') ?? 10),
      offset: Number(url.searchParams.get('offset') ?? 0),
      total: 1,
      has_more: false
    });
    return;
  }

  if (url.pathname === '/api/v1/memes/smoke-meme-1/analytics') {
    sendJson(response, 200, {
      meme_id: 'smoke-meme-1',
      window: url.searchParams.get('window') ?? '30d',
      start_at: '2025-12-03T00:00:00Z',
      end_at: '2026-01-02T00:00:00Z',
      granularity: 'day',
      history_start_at: '2025-12-31T18:00:00Z',
      history_end_at: '2026-01-02T00:00:00Z',
      refreshed_at: '2026-01-02T00:00:00Z',
      insufficient_history: false,
      summary: {
        totals: { source_views: 30, source_reactions: 2, source_reposts: 1, memeexpert_views: 12, memeexpert_sends: 3, memeexpert_saves: 2, memeexpert_favorites: 1, downloads: 4, recorded_activity: 51 },
        average_recorded_activity_per_day: 1.7,
        current_favorites: 7,
        momentum: { recent_recorded_activity: 40, previous_recorded_activity: 11, change: 29, change_rate: 2.636 },
        peak: { bucket_start: '2026-01-01T00:00:00Z', bucket_end: '2026-01-02T00:00:00Z', granularity: 'day', recorded_activity: 35 }
      },
      activity_points: [
        { bucket_start: '2025-12-31T00:00:00Z', bucket_end: '2026-01-01T00:00:00Z', granularity: 'day', source_views: 10, source_reactions: 1, source_reposts: 0, memeexpert_views: 2, memeexpert_sends: 1, memeexpert_saves: 1, memeexpert_favorites: 0, downloads: 1, recorded_activity: 15 },
        { bucket_start: '2026-01-01T00:00:00Z', bucket_end: '2026-01-02T00:00:00Z', granularity: 'day', source_views: 20, source_reactions: 1, source_reposts: 1, memeexpert_views: 10, memeexpert_sends: 2, memeexpert_saves: 1, memeexpert_favorites: 1, downloads: 3, recorded_activity: 36 }
      ],
      observed_source: {
        opening_baseline: { observed_at: '2025-12-31T18:00:00Z', views: 90, reactions: 4, comments: null, reposts: 2, coverage: smokeSourceCoverage },
        points: [{ observed_at: '2026-01-02T00:00:00Z', views: 120, reactions: 6, comments: null, reposts: 3, coverage: smokeSourceCoverage }]
      },
      source_performance: smokeSourceSummary,
      audience_change: { total_channels: 1, current_known_channels: 1, comparable_channels: 1, net_known_subscriber_change: 40 },
      exposure_funnels: {
        web: { recorded_card_impressions: 20, attributed_impressions: 18, matched_detail_clicks: 8, matched_high_intent_actions: 3, detail_click_rate: 0.4444, high_intent_rate: 0.1667 },
        telegram_inline: { inline_results_served: 10, attributed_results_served: 9, matched_chosen: 4, matched_sent: 3, chosen_rate: 0.4444, sent_rate: 0.3333 }
      }
    });
    return;
  }

  if (/^\/api\/v1\/memes\/[^/]+\/(?:detail-click|download|impression|share|view)$/.test(url.pathname)) {
    if (url.pathname.endsWith('/view')) detailViewCount += 1;
    sendJson(response, 200, { ok: true });
    return;
  }

  const similarMatch = url.pathname.match(/^\/api\/v1\/memes\/(smoke-meme-1|smoke-similar-1)\/similar$/);
  if (similarMatch) {
    const sourceMemeId = similarMatch[1];
    const limit = Number(url.searchParams.get('limit') ?? 12);
    const offset = Number(url.searchParams.get('offset') ?? 0);
    if (offset === 0) similarInitialReadCount += 1;
    const token = accessToken(request);
    if (
      sourceMemeId === 'smoke-meme-1' &&
      offset === 0 &&
      token?.startsWith('modal-full-') &&
      cookieValue(request, 'smoke_similar_revalidation_failure') === '1' &&
      !failedSimilarRefreshTokens.has(token)
    ) {
      failedSimilarRefreshTokens.add(token);
      sendJson(response, 503, { detail: 'Temporary Similar refresh failure.' });
      return;
    }
    const candidates = sourceMemeId === 'smoke-meme-1' ? similarMemes : [meme, nextMeme];
    const items = candidates.slice(offset, offset + limit).map((candidate, index) => ({
      meme: projectViewerMeme(request, candidate),
      attribution: similarAttribution(candidate.id, offset + index + 1, sourceMemeId)
    }));
    const payload = {
      items,
      limit,
      offset,
      total: candidates.length,
      has_more: offset + limit < candidates.length,
      request_id: `req_smoke_similar_${sourceMemeId}_${offset}`
    };
    if (sourceMemeId === 'smoke-meme-1' && offset > 0) {
      setTimeout(() => sendJson(response, 200, payload), 500);
    } else {
      sendJson(response, 200, payload);
    }
    return;
  }

  const memeActionMatch = url.pathname.match(/^\/api\/v1\/memes\/([^/]+)\/(favorite|save|pin)$/);
  if (memeActionMatch) {
    const stateResult = viewerState(request, { bootstrap: true });
    const { state } = stateResult;
    const action = memeActionMatch[2];
    if (action === 'favorite') {
      const nextFavorited = request.method !== 'DELETE';
      const changed = state.favorited !== nextFavorited;
      state.favorited = nextFavorited;
      sendJson(response, 200, {
        favorited: state.favorited,
        changed,
        like_count: meme.like_count + (state.favorited ? 1 : 0)
      }, stateResult.headers);
      return;
    }
    if (action === 'pin') {
      state.pinned = request.method !== 'DELETE';
    }
    sendJson(response, 200, request.method === 'DELETE' ? { removed: true } : { ok: true }, stateResult.headers);
    return;
  }

  const collectionMemeMatch = url.pathname.match(/^\/api\/v1\/collections\/([^/]+)\/memes\/([^/]+)$/);
  if (collectionMemeMatch && (request.method === 'POST' || request.method === 'DELETE')) {
    const stateResult = viewerState(request, { bootstrap: true });
    const collectionId = decodeURIComponent(collectionMemeMatch[1]);
    if (request.method === 'POST') stateResult.state.savedCollectionIds.add(collectionId);
    else stateResult.state.savedCollectionIds.delete(collectionId);
    sendJson(
      response,
      200,
      request.method === 'POST' ? { saved: true } : { removed: true },
      stateResult.headers
    );
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
  if (method === 'GET' && pathname === '/api/v1/admin/recovery/summary') {
    sendJson(response, 200, adminRecoverySummary);
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/recovery/work') {
    sendJson(response, 200, {
      items: adminRecoveryWork,
      next_cursor: null,
      snapshot_at: '2026-07-15T12:00:00Z'
    });
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/recovery/batches') {
    sendJson(response, 200, { items: [adminRecoveryJob], next_cursor: null, total: 1 });
    return true;
  }
  if (method === 'GET' && pathname === `/api/v1/admin/recovery/batches/${adminRecoveryJob.id}`) {
    sendJson(response, 200, adminRecoveryJob);
    return true;
  }
  if (method === 'GET' && pathname === `/api/v1/admin/recovery/batches/${adminRecoveryJob.id}/items`) {
    sendJson(response, 200, { items: adminRecoveryJobItems, next_cursor: null, total: adminRecoveryJobItems.length });
    return true;
  }
  if (method === 'POST' && pathname === '/api/v1/admin/recovery/batches/preview') {
    const body = await readRequestJson(request);
    if (!body || body.selector?.type !== 'query' || body.selector?.filters?.outdated_web_video !== true) {
      sendJson(response, 400, { detail: 'Smoke recovery preview requires the outdated-video query selector.' });
      return true;
    }
    sendJson(response, 201, {
      ...adminRecoveryJob,
      id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      request_id: body.request_id,
      status: 'preparing',
      action: body.action,
      scope: body.scope,
      retry_limit: body.retry_limit,
      reason: body.reason,
      total_count: 0,
      selected_root_count: 0,
      expanded_execution_count: 0,
      completed_count: 0,
      failed_count: 0,
      preparation_scanned_count: 1200,
      preparation_matched_count: 1000,
      preparation_excluded_count: 2,
      exclusion_groups: [{ reason: 'changed_since_snapshot', count: 2 }],
      scheduled_at: null,
      completed_at: null,
      version: 'preparing-version',
      items: []
    });
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/analytics/overview') {
    sendJson(response, 200, adminAnalyticsOverview);
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/analytics/engagement') {
    sendJson(response, 200, adminAnalyticsEngagement);
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/analytics/audience') {
    sendJson(response, 200, adminAnalyticsAudience);
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/analytics/content') {
    sendJson(response, 200, adminAnalyticsContent);
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/analytics/search-queries') {
    sendJson(response, 200, {
      range: adminAnalyticsRange,
      items: [adminAnalyticsQuery],
      total: 1,
      limit: Number(url.searchParams.get('limit') ?? 50),
      offset: Number(url.searchParams.get('offset') ?? 0)
    });
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/analytics/search-queries/detail') {
    if (url.searchParams.has('query') || url.searchParams.get('query_key') !== adminAnalyticsQuery.query_key) {
      sendJson(response, 422, { detail: 'The smoke API requires the opaque query_key parameter.' });
      return true;
    }
    sendJson(response, 200, {
      range: adminAnalyticsRange,
      ...adminAnalyticsQuery,
      meme_outcomes: [{ meme_id: adminIds.meme, interactions: 4, detail_clicks: 2, downloads: 1, saves: 1, shares: 0 }]
    });
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/channel-suggestions') {
    sendJson(response, 200, adminSuggestions);
    return true;
  }
  if (method === 'GET' && pathname === '/api/v1/admin/source-channels') {
    if (transientSourceRefreshFailures.delete(adminSources)) {
      sendJson(response, 503, { detail: 'Source catalog is restarting after creation.' });
      return true;
    }
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
  const sourceBackfillsMatch = method === 'GET'
    ? pathname.match(/^\/api\/v1\/admin\/source-channels\/([^/]+)\/backfills$/)
    : null;
  if (sourceBackfillsMatch) {
    const source = adminSources.find((candidate) => candidate.id === sourceBackfillsMatch[1]);
    if (!source) {
      sendJson(response, 404, { detail: 'Smoke source was not found.' });
      return true;
    }
    sendJson(response, 200, { items: [] });
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
    sendJson(response, 200, { meme: adminMeme, reports: [adminReport], decisions: [adminDecision], processing_files: adminMemeProcessingFiles });
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
    if (quickAdd.username === 'fresh_public_channel' && !transientQuickAddFailures.has(adminSources)) {
      transientQuickAddFailures.add(adminSources);
      sendJson(response, 504, { detail: 'Gateway restarted during source creation.' });
      return true;
    }
    const source = upsertQuickAddedSource(adminSources, quickAdd.username);
    transientSourceRefreshFailures.add(adminSources);
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

function recoveryWork(overrides = {}) {
  const work = {
    kind: 'pipeline_stage',
    id: 'smoke-recovery-work',
    bucket: 'retryable',
    title: 'Recovery work',
    source_label: null,
    source_channel_id: null,
    post_id: null,
    meme_file_id: null,
    stage: null,
    target: null,
    status: 'failed',
    reason: 'provider_unavailable',
    safe_error: 'The provider was temporarily unavailable.',
    error_code: 'provider_unavailable',
    is_retryable: true,
    attempt_count: 2,
    occurred_at: '2026-07-15T11:00:00Z',
    next_attempt_at: null,
    version: 'recovery-version',
    capabilities: ['retry_stage'],
    blocked_reason: null,
    details: {},
    ...overrides
  };
  work.actions ??= work.capabilities.map((capability) => ({ capability, available: true }));
  return work;
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
    latest_post_at: null,
    observed_post_count: 0,
    meme_count: 0,
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
    not_indexable_count: items.filter((item) => item.index_status === 'not_indexable').length,
    metadata_captured_count: items.filter((item) => item.metadata_state === 'captured').length,
    metadata_missing_count: items.filter((item) => item.metadata_state === 'missing').length
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
  return token === 'miniapp-full' || token?.startsWith('modal-full-') || token?.startsWith('smoke-full-') || hasAdminAccess(request);
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

function viewerState(request, { bootstrap = false } = {}) {
  let token = accessToken(request);
  const headers = {};
  if (!token && bootstrap) {
    token = `smoke-guest-state-${++guestStateSequence}`;
    headers['set-cookie'] = `memexpert_access_token=${token}; Path=/; HttpOnly; SameSite=Lax`;
  }
  if (!token) {
    return {
      state: { favorited: false, pinned: false, savedCollectionIds: new Set(), collectionChoiceReadCount: 0 },
      headers
    };
  }
  if (!viewerMemeStates.has(token)) {
    viewerMemeStates.set(token, {
      favorited: false,
      pinned: false,
      savedCollectionIds: new Set(),
      collectionChoiceReadCount: 0
    });
  }
  return { state: viewerMemeStates.get(token), headers };
}

function projectViewerMeme(request, sourceMeme) {
  if (sourceMeme.id !== meme.id) return sourceMeme;
  const { state } = viewerState(request);
  return {
    ...sourceMeme,
    like_count: meme.like_count + (state.favorited ? 1 : 0),
    viewer_has_favorited: state.favorited,
    viewer_has_saved: state.savedCollectionIds.size > 0,
    viewer_has_pinned: state.pinned
  };
}

function collectionChoice(summary, containsMeme) {
  return {
    collection_id: summary.collection.id,
    title: summary.collection.title,
    contains_meme: containsMeme,
    can_add_memes: summary.capabilities.can_add_memes,
    can_remove_memes: summary.capabilities.can_remove_memes
  };
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

function rankedMasonryMeme(index, caption, width, height, { tags = [`ranked-${index}`], mediaType = 'image', missingMedia = false } = {}) {
  const id = `smoke-ranked-masonry-${index}`;
  const isVideo = mediaType === 'video';
  const mediaUrl = isVideo
    ? `http://127.0.0.1:${port}/media/smoke-video.mp4`
    : `http://127.0.0.1:${port}/media/smoke-cat.svg`;
  const primaryFile = missingMedia
    ? null
    : {
        ...meme.primary_file,
        id: `${id}-file`,
        mime_type: isVideo ? 'video/mp4' : 'image/svg+xml',
        width,
        height,
        render: {
          ...meme.primary_file.render,
          display_url: `http://127.0.0.1:${port}/media/smoke-cat.svg`,
          original_url: mediaUrl,
          download_url: mediaUrl,
          web_video_url: isVideo ? mediaUrl : null,
          width,
          height
        },
        render_url: mediaUrl,
        download_url: mediaUrl
      };

  return {
    ...meme,
    id,
    media_type: mediaType,
    caption,
    seo_page_slug: `ranked-masonry-${index}`,
    tags,
    primary_file: primaryFile,
    render_url: missingMedia ? null : mediaUrl,
    download_url: missingMedia ? null : mediaUrl
  };
}

function rankedMasonryAttributionFor(url, memeId, rank) {
  return {
    request_id: 'req_smoke_ranked_masonry',
    impression_id: `imp_ranked_masonry_${rank}`,
    surface: 'search',
    source_algorithm: 'smoke-ranked-masonry',
    rank,
    query: url.searchParams.get('query') ?? null,
    filters: {
      language: url.searchParams.get('language'),
      media_type: url.searchParams.get('media_type'),
      include_nsfw: url.searchParams.get('include_nsfw') === 'true',
      tags: url.searchParams.getAll('tags'),
      scope: url.searchParams.get('scope'),
      collection_ids: url.searchParams.getAll('collection_ids')
    },
    collection_scope: url.searchParams.get('scope'),
    collection_ids: url.searchParams.getAll('collection_ids'),
    source_meme_id: null,
    algorithm_version: 'smoke-v1',
    score: rankedMasonryMemes.length - rank + 1,
    score_components: { backend_rank: rank },
    reason: 'Deterministic ranked masonry fixture'
  };
}

function homeAttribution(memeId, rank, attributionToken = `smoke-signed-home-${memeId}`) {
  return {
    request_id: 'req_smoke_home',
    impression_id: `imp_smoke_home_${memeId}`,
    surface: 'web_home',
    source_algorithm: 'personalized_recommendations',
    rank,
    algorithm_version: 'personalized_v2',
    profile_version: 'taste_v2:smoke',
    score: 1 - rank / 100,
    score_components: { total: 1 - rank / 100 },
    candidate_sources: [],
    reason: 'multi_source_personalized',
    attribution_token: attributionToken
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

function similarAttribution(memeId, rank, sourceMemeId) {
  return {
    request_id: `req_smoke_similar_${sourceMemeId}_${Math.floor((rank - 1) / 12) * 12}`,
    impression_id: `imp_similar_${memeId}`,
    surface: 'public_api_meme_similar',
    source_algorithm: 'qdrant_similarity',
    rank,
    query: null,
    filters: {
      language: null,
      media_type: null,
      include_nsfw: false,
      tags: [],
      scope: 'public',
      collection_ids: []
    },
    collection_scope: 'public',
    collection_ids: [],
    source_meme_id: sourceMemeId,
    algorithm_version: 'similar-smoke-v1',
    score: 1 - rank / 100,
    score_components: { similarity: 1 - rank / 100 },
    reason: 'similarity_match'
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
