export type ContentKind = 'audio' | 'gif' | 'image' | 'link' | 'text' | 'video';
export type ContentLanguage = 'en' | 'mixed' | 'none' | 'ru';
export type UserLanguage = 'any' | 'en' | 'ru';
export type AccountType = 'full' | 'guest';
export type CollectionKind = 'custom' | 'favorites';
export type CollectionVisibility = 'private' | 'public' | 'unlisted';
export type CollectionMembershipRole = 'editor' | 'owner' | 'viewer';
export type CollectionInviteChannel = 'direct_link' | 'email' | 'telegram';
export type CollectionInviteStatus = 'accepted' | 'expired' | 'pending' | 'revoked';
export type MemeSearchScope = 'all' | 'collections' | 'private' | 'public';

export interface UserRead {
  id: string;
  account_type: AccountType;
  telegram_id: number | null;
  google_id: string | null;
  email: string | null;
  email_verified_at: string | null;
  language: UserLanguage;
  nsfw_enabled: boolean;
  token_nonce: number;
  status: string;
  guest_expires_at: string | null;
  active_save_collection_id: string | null;
  is_admin: boolean;
  created_at: string;
  updated_at: string;
}

export interface LinkedProvidersRead {
  email: string | null;
  email_verified_at: string | null;
  has_password: boolean;
  google_linked: boolean;
  telegram_linked: boolean;
}

export interface CurrentSessionRead {
  user: UserRead;
  linked_providers: LinkedProvidersRead;
}

export interface ProfileStatsTagRead {
  tag: string;
  count: number;
}

export interface ProfileStatsTemplateRead {
  template_id: string;
  slug: string;
  name: string;
  count: number;
}

export interface ProfileStatsMetadataRead {
  notes: string[];
}

export interface ProfileStatsRead {
  viewed: number;
  sent: number;
  saved: number;
  downloaded: number;
  days_active: number;
  top_tags: ProfileStatsTagRead[];
  top_templates: ProfileStatsTemplateRead[];
  metadata: ProfileStatsMetadataRead;
}

export interface TelegramLinkStartRead {
  code: string;
  deep_link_url: string;
  expires_at: string;
  expires_in_seconds: number;
  return_url: string;
}
export type SourcePlatform = 'reddit' | 'telegram' | 'vk';
export type AdminSourceBackfillStatus =
  | 'cancelled'
  | 'completed'
  | 'completed_with_failures'
  | 'failed'
  | 'idle'
  | 'queued'
  | 'running'
  | 'waiting_capacity'
  | 'waiting_retry';
export type AdminSourcePostIndexStatus = 'failed' | 'indexed' | 'not_indexable' | 'partially_indexed' | 'processing';
export type AdminSourcePostSyncStatus = 'failed' | 'pending' | 'processing' | 'synced';
export type AdminRecoveryBucket = 'blocked' | 'dead_lettered' | 'retryable' | 'stuck';
export type AdminRecoveryWorkKind =
  | 'backfill'
  | 'dead_letter'
  | 'ingest_request'
  | 'outbox'
  | 'pipeline_stage'
  | 'source_post'
  | 'sync_target';
export type AdminRecoveryCapability =
  | 'archive_dead_letter'
  | 'regenerate_derivatives'
  | 'rebuild_outbox'
  | 'recover_dead_letter'
  | 'reinspect_ingest'
  | 'replay_stage'
  | 'replay_source_post'
  | 'resync_target'
  | 'resume_backfill'
  | 'retry_stage';
export type AdminRecoveryReplayScope = 'stage_and_dependents' | 'stage_only';
export type AdminRecoveryRetryLimit = 1 | 3 | 5;
export type AdminRecoveryBatchStatus =
  | 'cancelled'
  | 'cancelling'
  | 'completed'
  | 'completed_with_failures'
  | 'expired'
  | 'preparing'
  | 'preview'
  | 'queued'
  | 'running';
export type AdminRecoveryJobItemStatus =
  | 'cancelled'
  | 'dispatched'
  | 'failed'
  | 'queued'
  | 'skipped_dependency'
  | 'skipped_stale'
  | 'succeeded'
  | 'waiting_capacity'
  | 'waiting_dependency';
export type AdminSearchSynonymLocale = 'en' | 'ru';
export type AdminSearchSynonymRevisionStatus = 'archived' | 'draft' | 'published';
export type AdminSearchSynonymSyncStatus = 'failed' | 'idle' | 'pending' | 'synced' | 'syncing';
export type ChannelSuggestionStatus = 'approved' | 'pending' | 'rejected';
export type TelegramSessionStatus = 'active' | 'auth_required' | 'flood_wait' | 'quarantined' | 'stopped';
export type ModerationReportStatus = 'pending' | 'in_review' | 'resolved' | 'dismissed';
export type ModerationReason = 'copyright' | 'harassment' | 'illegal' | 'nsfw' | 'other' | 'spam';
export type ModerationAction =
  | 'hide'
  | 'hide_and_mark_nsfw'
  | 'mark_nsfw'
  | 'mark_sfw'
  | 'no_action'
  | 'template_override'
  | 'override_flags'
  | 'publish';

export interface PublicMemeFileRead {
  id: string;
  mime_type: string | null;
  width: number | null;
  height: number | null;
  file_size_bytes: number | null;
  blur_hash: string | null;
  quality_score: number;
  render: PublicMemeFileRenderRead | null;
  render_url?: string | null;
  download_url?: string | null;
}

export interface PublicMemeFileRenderRead {
  thumbnail_url: string | null;
  preview_url: string | null;
  display_url: string | null;
  original_url: string | null;
  download_url: string | null;
  web_video_url: string | null;
  width: number | null;
  height: number | null;
  blur_hash: string | null;
}

export interface PublicMemeCardRead {
  id: string;
  media_type: ContentKind;
  language: ContentLanguage;
  is_nsfw: boolean;
  popularity_score: number;
  like_count: number;
  tags: string[];
  primary_file: PublicMemeFileRead | null;
  caption: string | null;
  seo_page_slug: string | null;
  created_at: string;
  updated_at: string;
  render_url?: string | null;
  download_url?: string | null;
  viewer_has_favorited: boolean;
  viewer_has_saved: boolean;
  viewer_has_pinned: boolean;
  viewer_access?: PublicMemeViewerAccessRead | null;
}

export interface PublicMemeDetailRead extends PublicMemeCardRead {
  ocr_text: string | null;
  seo_page_slug: string | null;
  seo_title: string | null;
  seo_description: string | null;
  seo_alt_text: string | null;
  seo_body_text: string | null;
  seo_model_id: string | null;
  seo_prompt_version: string | null;
  seo_generated_at: string | null;
  files: PublicMemeFileRead[];
}

export interface MemeResultAttributionFiltersRead {
  language: ContentLanguage | null;
  media_type: ContentKind | null;
  include_nsfw: boolean;
  tags: string[];
  scope: string | null;
  collection_ids: string[];
}

export type MemeCandidateSource =
  | 'short_term'
  | 'current_intent'
  | 'long_term_global'
  | 'long_term_cluster'
  | 'multi_positive'
  | 'trending'
  | 'exploration'
  | 'visual_similarity'
  | 'tag_overlap'
  | 'same_template'
  | 'public_popular';

export interface MemeCandidateSourceContributionRead {
  source: MemeCandidateSource;
  rank: number;
  score: number | null;
  contribution: number;
}

export interface MemeResultAttributionRead {
  attribution_token: string | null;
  candidate_sources: MemeCandidateSourceContributionRead[];
  request_id: string | null;
  impression_id: string;
  surface: string | null;
  source_algorithm: string | null;
  rank: number | null;
  query: string | null;
  filters: MemeResultAttributionFiltersRead;
  collection_scope: string | null;
  collection_ids: string[];
  source_meme_id: string | null;
  algorithm_version: string | null;
  profile_version: string | null;
  score: number | null;
  score_components: Record<string, number>;
  reason: string | null;
}

export type PublicMemeViewerAccess = 'public' | 'private' | 'shared';

export interface PublicMemeViewerAccessRead {
  visibility: PublicMemeViewerAccess;
}

export interface PublicMemeSearchResultRead {
  meme: PublicMemeCardRead;
  attribution: MemeResultAttributionRead;
}

export interface PublicMemeOfTheDayRead {
  meme: PublicMemeCardRead | null;
  selected_for: string;
  refreshed_at: string;
  algorithm_version: string;
  score: number | null;
  score_components: Record<string, number>;
  reason: string;
  candidate_count: number;
  attribution: MemeResultAttributionRead | null;
}

export interface MemeFavoriteMutationRead {
  favorited: boolean;
  changed: boolean;
  like_count: number;
}

export interface PublicMemeSearchPageRead {
  items: PublicMemeSearchResultRead[];
  limit: number;
  offset: number;
  total: number;
  has_more: boolean;
  request_id: string;
}

export interface RecommendationFeedPageRead extends PublicMemeSearchPageRead {
  feed_session_id: string;
  next_cursor: string | null;
  expires_at: string;
}

export interface RecommendationFeedReauthorizationItemWrite {
  meme_id: string;
  attribution_token: string;
}

export interface RecommendationFeedReauthorizationRead {
  items: PublicMemeSearchResultRead[];
}

export type MemeInteractionBatchEventType = 'meme_impression' | 'meme_engaged_view' | 'meme_detail_click';

export interface MemeInteractionBatchEventWrite {
  event_id: string;
  event_type: MemeInteractionBatchEventType;
  meme_id: string;
  occurred_at: string;
  attribution_token: string | null;
  properties?: Record<string, unknown>;
}

export interface MemeInteractionBatchWrite {
  events: MemeInteractionBatchEventWrite[];
}

export interface MemeInteractionBatchRecordedRead {
  recorded: number;
  duplicates: number;
}

export interface CollectionSummaryRead {
  id: string;
  owner_id: string;
  title: string;
  description: string | null;
  kind: CollectionKind;
  visibility: CollectionVisibility;
  role: CollectionMembershipRole;
  can_write: boolean;
  saved_meme_count: number;
  created_at: string;
  updated_at: string;
}

export interface CollectionMemberRead {
  collection_id: string;
  user_id: string;
  role: CollectionMembershipRole;
  joined_at: string;
}

export interface CollectionInviteRead {
  id: string;
  collection_id: string;
  created_by_user_id: string | null;
  role: CollectionMembershipRole;
  channel: CollectionInviteChannel;
  label: string | null;
  status: CollectionInviteStatus;
  max_uses: number | null;
  use_count: number;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  recipient_email: string | null;
  created_at: string;
  updated_at: string;
}

export interface CollectionRead {
  id: string;
  owner_id: string;
  title: string;
  description: string | null;
  kind: CollectionKind;
  visibility: CollectionVisibility;
  memberships: CollectionMemberRead[];
  invites: CollectionInviteRead[];
  created_at: string;
  updated_at: string;
}

export interface MemeLibraryRead {
  favorites: PublicMemeCardRead[];
  pinned_memes: PublicMemeCardRead[];
  collections: CollectionSummaryRead[];
  active_save_collection: CollectionSummaryRead | null;
}

export interface PublicTrendCountsRead {
  views: number;
  sends: number;
  likes: number;
  saves: number;
  downloads: number;
}

export interface PublicTrendMetricsRead {
  recent: PublicTrendCountsRead;
  previous: PublicTrendCountsRead;
  latest_snapshot_at: string | null;
  latest_source_views: number;
  latest_source_reactions: number;
  latest_source_reposts: number;
  latest_platform_views: number;
  latest_platform_sends: number;
  latest_platform_saves: number;
  latest_platform_likes: number;
  latest_popularity_score: number;
  engagement_24h: number;
  trending_score: number;
  refreshed_at: string | null;
}

export interface PublicMemeTrendRead {
  meme: PublicMemeCardRead;
  trend: PublicTrendMetricsRead;
  attribution: MemeResultAttributionRead;
}

export interface PublicMemeTrendPageRead {
  items: PublicMemeTrendRead[];
  limit: number;
  offset: number;
  total: number;
  has_more: boolean;
  request_id: string;
}

export interface PublicMemePopularityPointRead {
  captured_at: string;
  source_views: number;
  source_reactions: number;
  source_reposts: number;
  platform_views: number;
  platform_sends: number;
  platform_saves: number;
  platform_likes: number;
  popularity_score: number;
}

export interface PublicMemePopularitySummaryRead {
  meme_id: string;
  trend: PublicTrendMetricsRead | null;
  sparkline: PublicMemePopularityPointRead[];
}

export type PublicMemeSourceSort =
  | 'views_desc'
  | 'reactions_desc'
  | 'reposts_desc'
  | 'interaction_rate_desc'
  | 'newest'
  | 'oldest';

export type PublicMemeAnalyticsWindow = '7d' | '30d' | '90d' | 'all';
export type PublicMemeAnalyticsGranularity = 'day' | 'week' | 'month' | 'adaptive';

export interface PublicMemeMetricCoverageRead {
  measured_posts: number;
  total_posts: number;
  ratio: number;
}

export interface PublicMemeSourceCoverageRead {
  views: PublicMemeMetricCoverageRead;
  reactions: PublicMemeMetricCoverageRead;
  comments: PublicMemeMetricCoverageRead;
  reposts: PublicMemeMetricCoverageRead;
}

export interface PublicMemeSourceTotalsRead {
  views: number | null;
  reactions: number | null;
  comments: number | null;
  reposts: number | null;
}

export interface PublicMemeSourceRateRead {
  value: number | null;
  numerator: number | null;
  denominator: number | null;
  eligible_posts: number;
  total_posts: number;
}

export interface PublicMemeSourceRatesRead {
  reactions: PublicMemeSourceRateRead;
  comments: PublicMemeSourceRateRead;
  reposts: PublicMemeSourceRateRead;
  interactions: PublicMemeSourceRateRead;
}

export interface PublicMemeSourceAudienceRead {
  audience_at_publish: number | null;
  current_audience: number | null;
  views_per_1000_subscribers: number | null;
  interactions_per_1000_subscribers: number | null;
}

export interface PublicMemeSourceAudienceSummaryRead {
  current_known_channels: number;
  total_channels: number;
  publish_time_eligible_posts: number;
  total_posts: number;
  views_per_1000_subscribers: PublicMemeSourceRateRead;
  interactions_per_1000_subscribers: PublicMemeSourceRateRead;
}

export interface PublicMemeSourcePostRead {
  channel_title: string;
  channel_username: string | null;
  channel_url: string | null;
  post_url: string | null;
  published_at: string | null;
  available: boolean;
  captured_at: string | null;
  views: number | null;
  reactions: number | null;
  comments: number | null;
  reposts: number | null;
  rates: PublicMemeSourceRatesRead;
  audience: PublicMemeSourceAudienceRead;
}

export interface PublicMemeSourceSummaryRead {
  total_posts: number;
  available_posts: number;
  distinct_channels: number;
  earliest_published_at: string | null;
  latest_published_at: string | null;
  latest_captured_at: string | null;
  totals: PublicMemeSourceTotalsRead;
  coverage: PublicMemeSourceCoverageRead;
  rates: PublicMemeSourceRatesRead;
  audience: PublicMemeSourceAudienceSummaryRead;
}

export interface PublicMemeSourcePageRead {
  meme_id: string;
  snapshot_at: string;
  sort: PublicMemeSourceSort;
  items: PublicMemeSourcePostRead[];
  summary: PublicMemeSourceSummaryRead;
  limit: number;
  offset: number;
  total: number;
  has_more: boolean;
}

export interface PublicMemeActivityCountsRead {
  source_views: number;
  source_reactions: number;
  source_reposts: number;
  memeexpert_views: number;
  memeexpert_sends: number;
  memeexpert_saves: number;
  memeexpert_favorites: number;
  downloads: number;
  recorded_activity: number;
}

export interface PublicMemeActivityPointRead extends PublicMemeActivityCountsRead {
  bucket_start: string;
  bucket_end: string;
  granularity: PublicMemeAnalyticsGranularity;
}

export interface PublicMemeAnalyticsMomentumRead {
  recent_recorded_activity: number;
  previous_recorded_activity: number;
  change: number;
  change_rate: number | null;
}

export interface PublicMemeAnalyticsPeakRead {
  bucket_start: string;
  bucket_end: string;
  granularity: PublicMemeAnalyticsGranularity;
  recorded_activity: number;
}

export interface PublicMemeAnalyticsSummaryRead {
  totals: PublicMemeActivityCountsRead;
  average_recorded_activity_per_day: number;
  current_favorites: number;
  momentum: PublicMemeAnalyticsMomentumRead;
  peak: PublicMemeAnalyticsPeakRead | null;
}

export interface PublicMemeObservedSourcePointRead extends PublicMemeSourceTotalsRead {
  observed_at: string;
  coverage: PublicMemeSourceCoverageRead;
}

export interface PublicMemeObservedSourceSeriesRead {
  opening_baseline: PublicMemeObservedSourcePointRead;
  points: PublicMemeObservedSourcePointRead[];
}

export interface PublicMemeWebExposureFunnelRead {
  recorded_card_impressions: number;
  attributed_impressions: number;
  matched_detail_clicks: number;
  matched_high_intent_actions: number;
  detail_click_rate: number | null;
  high_intent_rate: number | null;
}

export interface PublicMemeInlineExposureFunnelRead {
  inline_results_served: number;
  attributed_results_served: number;
  matched_chosen: number;
  matched_sent: number;
  chosen_rate: number | null;
  sent_rate: number | null;
}

export interface PublicMemeExposureFunnelsRead {
  web: PublicMemeWebExposureFunnelRead;
  telegram_inline: PublicMemeInlineExposureFunnelRead;
}

export interface PublicMemeChannelAudienceChangeRead {
  total_channels: number;
  current_known_channels: number;
  comparable_channels: number;
  net_known_subscriber_change: number | null;
}

export interface PublicMemeAnalyticsRead {
  meme_id: string;
  window: PublicMemeAnalyticsWindow;
  start_at: string;
  end_at: string;
  granularity: PublicMemeAnalyticsGranularity;
  history_start_at: string | null;
  history_end_at: string | null;
  refreshed_at: string;
  insufficient_history: boolean;
  summary: PublicMemeAnalyticsSummaryRead;
  activity_points: PublicMemeActivityPointRead[];
  observed_source: PublicMemeObservedSourceSeriesRead;
  source_performance: PublicMemeSourceSummaryRead;
  audience_change: PublicMemeChannelAudienceChangeRead;
  exposure_funnels: PublicMemeExposureFunnelsRead;
}

export interface PublicTrendAggregatePointRead {
  observed_at: string | null;
  value: number;
  metric: string;
  label: string;
  meme_count?: number;
  snapshot_count?: number;
  source_views?: number;
  source_reactions?: number;
  source_reposts?: number;
  platform_views?: number;
  platform_sends?: number;
  platform_saves?: number;
  platform_likes?: number;
}

export interface PublicTrendComparisonPointRead extends PublicTrendAggregatePointRead {}

export interface PublicTrendComparisonSeriesRead {
  kind: 'meme' | 'tag' | 'template' | 'unknown' | string;
  value: string;
  title: string;
  description: string | null;
  meme: PublicMemeCardRead | null;
  trend: PublicTrendMetricsRead | null;
  points: PublicTrendComparisonPointRead[];
  insufficient_history: boolean;
  no_data_reason: string | null;
  current_only_reason?: string | null;
}

export interface PublicTrendComparisonRead {
  items: PublicTrendComparisonSeriesRead[];
  requested_items: string[];
  max_items: number;
}

export interface PublicTrendSummaryRead {
  kind: 'tag' | 'template' | string;
  slug: string;
  title: string;
  description: string | null;
  meme_count: number;
  trend: PublicTrendMetricsRead;
  points?: PublicTrendAggregatePointRead[];
  insufficient_history?: boolean;
  no_data_reason?: string | null;
  current_only_reason?: string | null;
}

export interface PublicTrendTimelineMemeRead {
  meme: PublicMemeCardRead;
  popularity_score: number;
  snapshot_count: number;
  first_captured_at: string;
  last_captured_at: string;
  source_views: number;
  source_reactions: number;
  source_reposts: number;
  platform_views: number;
  platform_sends: number;
  platform_saves: number;
  platform_likes: number;
}

export interface PublicTrendTimelinePeriodRead {
  period: string;
  period_start: string;
  top_memes: PublicTrendTimelineMemeRead[];
  meme_count: number;
  snapshot_count: number;
}

export interface PublicTrendTimelinePageRead {
  granularity: 'month' | 'year' | string;
  periods: PublicTrendTimelinePeriodRead[];
  limit: number;
  offset: number;
  total: number;
  has_more: boolean;
}

export interface CollectionCapabilitiesRead {
  can_view: boolean;
  can_add_memes: boolean;
  can_remove_memes: boolean;
  can_rename: boolean;
  can_delete: boolean;
  can_create_invites: boolean;
  can_revoke_invites: boolean;
  can_manage_members: boolean;
  can_set_active_save: boolean;
}

export interface PinnedMemeRead {
  user_id: string;
  meme_id: string;
  position: number;
  pinned_at: string;
}

export interface WebCollectionSummaryRead {
  collection: CollectionRead;
  viewer_role: CollectionMembershipRole;
  capabilities: CollectionCapabilitiesRead;
  active_save_collection_id: string | null;
}

export interface CollectionMemeRead {
  collection_id: string;
  meme_id: string;
  added_by_user_id: string | null;
  added_at: string;
}

export interface CollectionSavedMemeRead {
  save: CollectionMemeRead;
  meme: PublicMemeCardRead;
}

export interface WebCollectionDetailRead extends WebCollectionSummaryRead {
  saved_memes: CollectionSavedMemeRead[];
}

export interface WebCollectionListRead {
  collections: WebCollectionSummaryRead[];
  active_save_collection_id: string | null;
}

export interface MemeCollectionChoiceRead {
  collection_id: string;
  title: string;
  contains_meme: boolean;
  can_add_memes: boolean;
  can_remove_memes: boolean;
}

export interface MemeCollectionChoicesRead {
  collections: MemeCollectionChoiceRead[];
}

export interface CollectionInviteLinkRead {
  invite: CollectionInviteRead;
  token: string;
  join_path: string;
}

export interface PublicMemeLandingRead {
  kind: 'tag' | 'template' | string;
  slug: string;
  title: string;
  description: string | null;
  page: PublicMemeSearchPageRead;
  trend_summary: PublicTrendSummaryRead | null;
}

export interface SeoCatalogSummaryRead {
  public_safe_meme_count: number;
  tag_count: number;
  template_count: number;
  updated_at: string | null;
}

export interface SeoCatalogMemeTemplateRefRead {
  slug: string;
  name: string;
  title: string;
  description: string | null;
}

export interface SeoCatalogMemeRead {
  id: string;
  seo_slug: string | null;
  title: string;
  description: string | null;
  alt_text: string;
  caption: string | null;
  tags: string[];
  media_type: ContentKind;
  language: ContentLanguage;
  popularity_score: number;
  like_count: number;
  template: SeoCatalogMemeTemplateRefRead | null;
  primary_file: PublicMemeFileRead | null;
  files: PublicMemeFileRead[];
  created_at: string;
  updated_at: string;
}

export interface SeoCatalogMemePageRead {
  items: SeoCatalogMemeRead[];
  limit: number;
  offset: number;
  total: number;
  has_more: boolean;
}

export interface SeoCatalogTagRead {
  slug: string;
  title: string;
  description: string | null;
  meme_count: number;
  updated_at: string | null;
}

export interface SeoCatalogTagPageRead {
  items: SeoCatalogTagRead[];
  limit: number;
  offset: number;
  total: number;
  has_more: boolean;
}

export interface SeoCatalogTemplateRead {
  slug: string;
  name: string;
  title: string;
  description: string | null;
  meme_count: number;
  updated_at: string | null;
}

export interface SeoCatalogTemplatePageRead {
  items: SeoCatalogTemplateRead[];
  limit: number;
  offset: number;
  total: number;
  has_more: boolean;
}

export interface AdminSessionRead {
  user: UserRead;
}

export interface AdminOverviewRead {
  open_report_count: number;
  pending_suggestion_count: number;
  source_attention_count: number;
  orphaned_source_count: number;
  stale_source_count: number;
  waiting_source_count: number;
  healthy_source_count: number;
  telegram_account_attention_count: number;
  ready_telegram_account_count: number;
  missing_seo_count: number;
  uncurated_template_count: number;
}

export type AdminAnalyticsBucket = 'day';

/**
 * A resolved, inclusive UTC reporting window. The backend is authoritative for
 * the default range and for comparison-period calculation.
 */
export interface AdminAnalyticsRangeRead {
  start_date: string;
  end_date: string;
  comparison_start_date: string;
  comparison_end_date: string;
  timezone: 'UTC';
  bucket: AdminAnalyticsBucket;
}

export interface AdminAnalyticsMetricRead {
  value: number;
  previous_value: number;
  change: number;
  change_percent: number | null;
}

/** One named aggregate suitable for a bar chart, donut, or compact table. */
export interface AdminAnalyticsBreakdownRead {
  key: string;
  count: number;
}

export interface AdminAnalyticsSurfaceRead {
  surface: string;
  count: number;
}

export interface AdminAnalyticsOverviewActivityRead {
  date: string;
  page_views: number;
  active_users: number;
  interactions: number;
  searches: number;
  downloads: number;
  new_memes: number;
}

export interface AdminAnalyticsEngagementActivityRead {
  date: string;
  interactions: number;
  searches: number;
  zero_result_searches: number;
  detail_clicks: number;
  downloads: number;
  sends: number;
  saves: number;
  shares: number;
}

export interface AdminAnalyticsAudienceActivityRead {
  date: string;
  new_guests: number;
  new_full_accounts: number;
  active_users: number;
  guest_to_full_conversions: number;
}

export interface AdminAnalyticsCatalogGrowthRead {
  date: string;
  new_memes: number;
}

export interface AdminAnalyticsSourceEngagementRead {
  date: string;
  source_views: number;
  source_reactions: number;
  source_reposts: number;
}

export interface AdminAnalyticsRetentionPeriodRead {
  eligible_users: number;
  retained_users: number;
  rate: number | null;
}

export interface AdminAnalyticsRetentionCohortRead {
  cohort_date: string;
  cohort_size: number;
  d1: AdminAnalyticsRetentionPeriodRead | null;
  d7: AdminAnalyticsRetentionPeriodRead | null;
  d30: AdminAnalyticsRetentionPeriodRead | null;
}

export interface AdminAnalyticsSearchQueryRead {
  /** Opaque HMAC-derived identifier used for drill-down URLs; never raw query text. */
  query_key: string;
  query: string;
  searches: number;
  zero_result_searches: number;
  zero_result_rate: number | null;
  average_latency_ms: number | null;
  detail_clicks: number;
  downloads: number;
}

export type AdminAnalyticsSearchQuerySort = 'searches' | 'niche' | 'zero_result_rate' | 'downloads';

export interface AdminAnalyticsMemeOutcomeRead {
  meme_id: string;
  interactions: number;
  detail_clicks: number;
  downloads: number;
  saves: number;
  shares: number;
}

export interface AdminAnalyticsOverviewRead {
  range: AdminAnalyticsRangeRead;
  metrics: Record<string, AdminAnalyticsMetricRead>;
  activity: AdminAnalyticsOverviewActivityRead[];
  discovery_funnel: {
    searches: number;
    searches_with_results: number;
    searches_without_results: number;
    detail_clicks: number;
    downloads: number;
  };
  surface_mix: AdminAnalyticsSurfaceRead[];
  source_activity: {
    sources: number;
    new_sources: number;
    source_views: number;
    source_reactions: number;
    source_reposts: number;
  };
}

export interface AdminAnalyticsEngagementRead {
  range: AdminAnalyticsRangeRead;
  metrics: Record<string, AdminAnalyticsMetricRead>;
  activity: AdminAnalyticsEngagementActivityRead[];
  interactions_by_type: AdminAnalyticsBreakdownRead[];
  surface_mix: AdminAnalyticsSurfaceRead[];
  top_search_queries: AdminAnalyticsSearchQueryRead[];
}

export interface AdminAnalyticsAudienceRead {
  range: AdminAnalyticsRangeRead;
  metrics: Record<string, AdminAnalyticsMetricRead>;
  activity: AdminAnalyticsAudienceActivityRead[];
  surface_mix: AdminAnalyticsSurfaceRead[];
  retention_cohorts: AdminAnalyticsRetentionCohortRead[];
}

export interface AdminAnalyticsContentRead {
  range: AdminAnalyticsRangeRead;
  metrics: Record<string, AdminAnalyticsMetricRead>;
  catalog_growth: AdminAnalyticsCatalogGrowthRead[];
  media_types: AdminAnalyticsBreakdownRead[];
  languages: AdminAnalyticsBreakdownRead[];
  visibility: AdminAnalyticsBreakdownRead[];
  processing: AdminAnalyticsBreakdownRead[];
  source_health: AdminAnalyticsBreakdownRead[];
  source_engagement: AdminAnalyticsSourceEngagementRead[];
}

export interface AdminAnalyticsSearchQueryPageRead {
  range: AdminAnalyticsRangeRead;
  items: AdminAnalyticsSearchQueryRead[];
  total: number;
  limit: number;
  offset: number;
}

export interface AdminAnalyticsSearchQueryDetailRead {
  range: AdminAnalyticsRangeRead;
  query: string;
  query_key: string;
  searches: number;
  zero_result_searches: number;
  zero_result_rate: number | null;
  average_latency_ms: number | null;
  meme_outcomes: AdminAnalyticsMemeOutcomeRead[];
}

export interface ChannelSuggestionRead {
  id: string;
  user_id: string;
  platform: SourcePlatform;
  channel_url: string;
  status: ChannelSuggestionStatus;
  admin_note: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AdminSourceChannelRead {
  id: string;
  platform: SourcePlatform;
  platform_id: string;
  username: string | null;
  title: string;
  subscriber_count: number | null;
  is_active: boolean;
  is_paused: boolean;
  catchup_enabled: boolean;
  live_enabled: boolean;
  engagement_enabled: boolean;
  catchup_message_limit: number;
  telegram_session_id: string | null;
  telegram_session_name: string | null;
  is_orphaned: boolean;
  is_indexable: boolean;
  last_read_post_id: string | null;
  oldest_observed_post_id: string | null;
  initial_catchup_completed: boolean;
  history_exhausted: boolean;
  backfill_status: AdminSourceBackfillStatus;
  backfill_requested_count: number;
  backfill_scanned_count: number;
  backfill_error: string | null;
  latest_post_at: string | null;
  observed_post_count: number;
  meme_count: number;
  last_fetched_at: string | null;
  operational_status: 'active' | 'inactive' | 'paused';
  freshness_status: 'checkpoint_only' | 'fresh' | 'never_fetched' | 'stale';
  seconds_since_last_fetch: number | null;
  created_at: string;
  updated_at: string;
}

export interface AdminSourcePostSummaryRead {
  observed_count: number;
  indexed_count: number;
  partially_indexed_count: number;
  processing_count: number;
  failed_count: number;
  not_indexable_count: number;
  metadata_captured_count: number;
  metadata_missing_count: number;
}

export interface AdminSourcePostRead {
  id: string;
  post_id: string;
  telegram_url: string | null;
  published_at: string | null;
  observed_at: string;
  media_type: string | null;
  metadata_state: 'captured' | 'missing';
  text_excerpt: string | null;
  media_group_id: string | null;
  reply_to_post_id: string | null;
  telegram_edited_at: string | null;
  metadata_first_observed_at: string | null;
  metadata_last_observed_at: string | null;
  is_deleted: boolean;
  deletion_observed_at: string | null;
  fetch_status: string;
  fetch_detail: string | null;
  ingest_outcome: string | null;
  ingest_status: string | null;
  meme_id: string | null;
  meme_file_id: string | null;
  pipeline_stage: string | null;
  pipeline_status: string | null;
  pipeline_error: string | null;
  qdrant_status: AdminSourcePostSyncStatus | null;
  meilisearch_status: AdminSourcePostSyncStatus | null;
  index_status: AdminSourcePostIndexStatus;
  is_retryable: boolean;
  version: string;
  capabilities: AdminRecoveryCapability[];
  blocked_reason: string | null;
}

export interface AdminSourcePostPageRead {
  source_channel_id: string;
  snapshot_at: string;
  summary: AdminSourcePostSummaryRead;
  items: AdminSourcePostRead[];
  total: number;
  limit: number;
  offset: number;
}

export interface AdminSourceBackfillPayload {
  message_limit: number;
}

export interface AdminSourceBackfillRead {
  id: string;
  source_channel_id: string;
  status: AdminSourceBackfillStatus;
  requested_count: number;
  scanned_count: number;
  remaining_count: number;
  cursor_post_id: string | null;
  attempt_count: number;
  quarantined_count: number;
  last_error_code: string | null;
  last_error_class: string | null;
  safe_error: string | null;
  is_retryable: boolean;
  next_attempt_at: string | null;
  last_progress_at: string | null;
  telegram_session_id: string | null;
  telegram_session_name: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string;
  version: string;
  capabilities: AdminRecoveryCapability[];
}

export interface AdminSourceBackfillListRead {
  items: AdminSourceBackfillRead[];
}

export interface AdminRecoverySummaryRead {
  retryable_count: number;
  blocked_count: number;
  stuck_count: number;
  dead_lettered_count: number;
  outdated_web_video_count?: number;
  active_job_count?: number;
  preparing_job_count?: number;
  snapshot_at?: string;
}

export interface AdminRecoveryBlockedPrerequisiteRead {
  code?: string;
  message: string;
}

export interface AdminRecoveryRiskRead {
  code?: string;
  message: string;
  severity?: 'danger' | 'info' | 'warning';
}

export interface AdminRecoveryAcknowledgementRead {
  key: string;
  label: string;
}

export interface AdminRecoveryActionScopeRequirementsRead {
  warnings?: string[];
  risks?: Array<AdminRecoveryRiskRead | string>;
  required_acknowledgements?: Array<AdminRecoveryAcknowledgementRead | string>;
}

export interface AdminRecoveryActionCandidateRead {
  capability: AdminRecoveryCapability;
  /** Compatibility alias accepted while coordinated backend changes roll out. */
  action?: AdminRecoveryCapability;
  available: boolean;
  scopes?: AdminRecoveryReplayScope[];
  default_scope?: AdminRecoveryReplayScope | null;
  retry_limits?: AdminRecoveryRetryLimit[];
  default_retry_limit?: AdminRecoveryRetryLimit;
  downstream_stages?: string[];
  warnings?: string[];
  risks?: Array<AdminRecoveryRiskRead | string>;
  required_acknowledgements?: Array<AdminRecoveryAcknowledgementRead | string>;
  scope_requirements?: Partial<
    Record<AdminRecoveryReplayScope, AdminRecoveryActionScopeRequirementsRead>
  >;
  blocked_prerequisites?: Array<AdminRecoveryBlockedPrerequisiteRead | string>;
}

export interface AdminRecoveryWorkRead {
  kind: AdminRecoveryWorkKind;
  id: string;
  bucket: AdminRecoveryBucket;
  title: string;
  source_label: string | null;
  source_channel_id: string | null;
  post_id: string | null;
  meme_file_id: string | null;
  stage: string | null;
  target: string | null;
  status: string;
  reason: string | null;
  safe_error: string | null;
  error_code: string | null;
  is_retryable: boolean;
  attempt_count: number;
  occurred_at: string;
  next_attempt_at: string | null;
  version: string;
  capabilities: AdminRecoveryCapability[];
  actions: AdminRecoveryActionCandidateRead[];
  blocked_reason: string | null;
  details: Record<string, string | number | boolean | null>;
  available_actions?: AdminRecoveryActionCandidateRead[];
  warnings?: string[];
  risks?: Array<AdminRecoveryRiskRead | string>;
  web_video_profile?: string | null;
  active_job?: AdminRecoveryActiveJobRead | AdminRecoveryJobSummaryRead | null;
}

export interface AdminRecoveryWorkPageRead {
  items: AdminRecoveryWorkRead[];
  next_cursor: string | null;
  snapshot_at: string;
}

export interface AdminRecoveryMutationPayload {
  request_id: string;
  version: string;
  reason: string;
  capability: AdminRecoveryCapability;
}

export interface AdminRecoveryActionPayload {
  request_id: string;
  version: string;
  reason: string;
  action: AdminRecoveryCapability;
  scope: AdminRecoveryReplayScope;
  retry_limit: AdminRecoveryRetryLimit;
  acknowledgements: string[];
}

export interface AdminRecoveryMediaProfileRead {
  active_object_key?: string | null;
  profile: string | null;
  verified_at?: string | null;
  source_has_audio?: boolean | null;
  web_video_has_audio?: boolean | null;
  outdated?: boolean;
}

export interface AdminRecoveryActiveJobRead {
  id: string;
  status: AdminRecoveryBatchStatus | string;
  action: AdminRecoveryCapability;
  scope?: AdminRecoveryReplayScope;
  requested_by_admin_user_id?: string;
  assigned_admin_user_id?: string | null;
  created_at?: string;
}

export interface AdminRecoveryCandidateRead {
  work: AdminRecoveryWorkRead;
  version?: string;
  actions: AdminRecoveryActionCandidateRead[];
  warnings?: string[];
  risks?: Array<AdminRecoveryRiskRead | string>;
  media_profile?: AdminRecoveryMediaProfileRead | string | null;
  active_job?: AdminRecoveryActiveJobRead | null;
}

export interface AdminRecoveryWorkReference {
  kind: AdminRecoveryWorkKind;
  id: string;
  version: string;
}

export interface AdminRecoveryExplicitSelector {
  type: 'explicit';
  items: AdminRecoveryWorkReference[];
}

export interface AdminRecoveryQuerySelector {
  type: 'query';
  filters: Record<string, string | number | boolean | string[] | null>;
  snapshot_at: string;
}

export type AdminRecoveryBatchSelector = AdminRecoveryExplicitSelector | AdminRecoveryQuerySelector;

export interface AdminRecoveryBatchPreviewPayload {
  request_id: string;
  reason: string;
  action: AdminRecoveryCapability;
  scope: AdminRecoveryReplayScope;
  retry_limit: AdminRecoveryRetryLimit;
  selector: AdminRecoveryBatchSelector;
  acknowledgements?: string[];
  /** Legacy fields remain readable during the coordinated rollout. */
  capability?: AdminRecoveryCapability;
  items?: AdminRecoveryWorkReference[];
}

export interface AdminRecoveryBatchMutationPayload {
  version: string;
  reason: string;
}

export interface AdminRecoveryRetryFailedPreviewPayload {
  request_id: string;
  version: string;
  reason: string;
  retry_limit: AdminRecoveryRetryLimit;
}

export interface AdminRecoveryBatchHandoffPayload {
  version: string;
  reason: string;
  assigned_admin_user_id: string;
}

export interface AdminRecoveryJobItemRead {
  id: string;
  source_item_id?: string | null;
  work_kind: AdminRecoveryWorkKind;
  work_id: string;
  meme_file_id?: string | null;
  action: AdminRecoveryCapability;
  status: AdminRecoveryJobItemStatus;
  stage?: string | null;
  parent_item_id?: string | null;
  root_item_id?: string | null;
  is_root?: boolean;
  attempt_count?: number;
  retry_limit?: AdminRecoveryRetryLimit;
  attempt_budget_start?: number;
  retryable_failures_consumed?: number;
  expected_version?: string | null;
  canonical_version?: string | null;
  normalized_reason: string | null;
  safe_error: string | null;
  dispatched_at: string | null;
  finished_at: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AdminRecoveryExclusionGroupRead {
  reason: string;
  count: number;
  message?: string;
}

export interface AdminRecoveryJobSummaryRead {
  id: string;
  request_id: string;
  status: AdminRecoveryBatchStatus;
  action: AdminRecoveryCapability;
  scope?: AdminRecoveryReplayScope;
  retry_limit?: AdminRecoveryRetryLimit;
  reason: string;
  total_count: number;
  completed_count: number;
  failed_count: number;
  selected_root_count?: number;
  expanded_execution_count?: number;
  preparation_scanned_count?: number;
  preparation_matched_count?: number;
  preparation_excluded_count?: number;
  excluded_count?: number;
  queued_count?: number;
  waiting_count?: number;
  waiting_capacity_count?: number;
  waiting_dependency_count?: number;
  dispatched_count?: number;
  succeeded_count?: number;
  stale_count?: number;
  skipped_count?: number;
  skipped_dependency_count?: number;
  cancelled_count?: number;
  exclusion_groups?: AdminRecoveryExclusionGroupRead[];
  exclusions?: Record<string, number>;
  exclusions_by_reason?: Record<string, number>;
  requested_by_admin_user_id?: string;
  requested_by_display_name?: string | null;
  assigned_to_admin_user_id?: string | null;
  assigned_admin_user_id?: string | null;
  assigned_to_display_name?: string | null;
  can_handoff?: boolean;
  source_job_id?: string | null;
  source_recovery_job_id?: string | null;
  selection_snapshot_at?: string | null;
  materialization_completed_at?: string | null;
  expires_at: string | null;
  scheduled_at: string | null;
  completed_at: string | null;
  cancelled_at: string | null;
  created_at: string;
  updated_at: string;
  version: string;
}

export interface AdminRecoveryBatchRead extends AdminRecoveryJobSummaryRead {
  items?: AdminRecoveryJobItemRead[];
}

export interface AdminRecoveryJobPageRead {
  items: AdminRecoveryJobSummaryRead[];
  next_cursor: string | null;
  total?: number;
}

export interface AdminRecoveryJobItemPageRead {
  items: AdminRecoveryJobItemRead[];
  next_cursor: string | null;
  total?: number;
}

export interface AdminSearchSynonymValidationIssue {
  level: 'error' | 'warning';
  code: string;
  message: string;
  line_number: number | null;
  term: string | null;
}

export interface AdminSearchSynonymValidationRead {
  valid: boolean;
  group_count: number;
  compiled_key_count: number;
  edge_count: number;
  payload_bytes: number;
  issues: AdminSearchSynonymValidationIssue[];
}

export interface AdminSearchSynonymRevisionRead {
  id: string;
  revision_number: number;
  status: AdminSearchSynonymRevisionStatus;
  source_text: string;
  compiler_version: string;
  compiled_hash: string | null;
  validation: AdminSearchSynonymValidationRead;
  change_note: string | null;
  published_at: string | null;
  created_at: string;
  updated_at: string;
  version: string;
}

export interface AdminSearchSynonymCatalogRead {
  locale: AdminSearchSynonymLocale;
  draft: AdminSearchSynonymRevisionRead;
  published: AdminSearchSynonymRevisionRead | null;
  history: AdminSearchSynonymRevisionRead[];
}

export interface AdminSearchSynonymSyncStateRead {
  index_name: string;
  status: AdminSearchSynonymSyncStatus;
  desired_hash: string | null;
  applied_hash: string | null;
  actual_hash: string | null;
  desired_revisions: Partial<Record<AdminSearchSynonymLocale, number>>;
  last_task_uid: number | null;
  requested_at: string | null;
  last_checked_at: string | null;
  last_applied_at: string | null;
  safe_error: string | null;
  updated_at: string | null;
  version: string;
}

export interface AdminSearchSynonymDraftUpdatePayload {
  request_id: string;
  version: string;
  source_text: string;
  reason: string;
}

export interface AdminSearchSynonymMutationPayload {
  request_id: string;
  version: string;
  reason: string;
}

export interface AdminSearchSynonymPublishPayload extends AdminSearchSynonymMutationPayload {
  confirm_destructive: boolean;
}

export interface AdminSearchSynonymResetPayload extends AdminSearchSynonymMutationPayload {
  revision_id?: string | null;
}

export interface AdminSearchSynonymSyncRetryPayload {
  request_id: string;
  version: string;
  reason: string;
}

export interface AdminSourceRecoveryMutationPayload {
  request_id: string;
  version: string;
  reason: string;
}

export interface AdminTelegramSessionRead {
  id: string;
  name: string;
  display_name: string;
  owned_channel_count: number;
  status: TelegramSessionStatus;
  enabled: boolean;
  flood_wait_until: string | null;
  live_listener_started_at: string | null;
  last_heartbeat_at: string | null;
  last_error_class: string | null;
  last_error_text: string | null;
  quarantined_at: string | null;
  live_enabled: boolean;
  catchup_enabled: boolean;
  engagement_enabled: boolean;
  max_requests_per_second: number;
  account_user_id: number | null;
  account_username: string | null;
  account_phone_hint: string | null;
  has_string_session: boolean;
  created_at: string;
  updated_at: string;
}

export interface AdminTelegramChannelGroupRead {
  telegram_session: AdminTelegramSessionRead | null;
  is_orphaned: boolean;
  channels: AdminSourceChannelRead[];
}

export interface AdminTelegramSessionCreatePayload {
  name?: string | null;
  display_name?: string | null;
  enabled: boolean;
  live_enabled: boolean;
  catchup_enabled: boolean;
  engagement_enabled: boolean;
  max_requests_per_second: number;
  note?: string | null;
}

export interface AdminTelegramSessionUpdatePayload {
  display_name?: string;
  enabled?: boolean;
  status?: TelegramSessionStatus;
  live_enabled?: boolean;
  catchup_enabled?: boolean;
  engagement_enabled?: boolean;
  max_requests_per_second?: number;
  flood_wait_until?: string | null;
  last_error_class?: string | null;
  last_error_text?: string | null;
  clear_error?: boolean;
  note?: string | null;
}

export interface AdminTelegramSessionValidatePayload {
  source_channel_id?: string | null;
  note?: string | null;
}

export interface AdminTelegramSessionValidateRead {
  telegram_session: AdminTelegramSessionRead;
  channel_checked: boolean;
  channel_reference: string | null;
}

export interface AdminTelegramLoginQrStartRead {
  attempt_id: string;
  qr_url: string;
  expires_at: string;
  message: string;
}

export interface AdminTelegramLoginQrStartPayload {
  telegram_session_id?: string | null;
  note?: string | null;
}

export interface AdminTelegramLoginQrCompletePayload {
  note?: string | null;
}

export type AdminTelegramQrLoginStatus = 'pending' | 'completed' | 'password_required';

export interface AdminTelegramLoginQrStatusRead {
  status: AdminTelegramQrLoginStatus;
  telegram_session: AdminTelegramSessionRead | null;
  password_required: boolean;
  message: string;
}

export interface AdminTelegramLoginPhoneStartPayload {
  telegram_session_id?: string | null;
  phone_number: string;
  note?: string | null;
}

export interface AdminTelegramLoginPhoneStartRead {
  attempt_id: string;
  phone_number_hint: string | null;
  expires_at: string;
  message: string;
}

export interface AdminTelegramLoginPhoneCodePayload {
  code: string;
  note?: string | null;
}

export interface AdminTelegramLoginPasswordPayload {
  password: string;
  note?: string | null;
}

export interface AdminTelegramLoginCompleteRead {
  telegram_session: AdminTelegramSessionRead | null;
  password_required: boolean;
  message: string;
}

export interface AdminTelegramLoginCancelRead {
  attempt_id: string;
  status: 'cancelled';
  message: string;
}

export interface AdminTelegramSessionDeletePayload {
  confirmation: string;
  note?: string | null;
}

export interface AdminTelegramSessionActionRead {
  action: 'delete';
  telegram_session_id: string;
  orphaned_source_channel_count: number;
  message: string;
}

export interface AdminTelegramChannelCreatePayload {
  platform: 'telegram';
  platform_id: string;
  username?: string | null;
  title: string;
  subscriber_count?: number | null;
  telegram_session_id?: string | null;
  orphaned?: boolean;
  catchup_enabled: boolean;
  live_enabled: boolean;
  engagement_enabled: boolean;
  catchup_message_limit: number;
}

export interface AdminTelegramChannelFromReferencePayload {
  reference: string;
  telegram_session_id: string;
  suggestion_id?: string | null;
  catchup_message_limit?: number;
}

export interface AdminTelegramChannelUpdatePayload {
  catchup_enabled?: boolean;
  live_enabled?: boolean;
  engagement_enabled?: boolean;
  catchup_message_limit?: number;
}

export interface AdminTelegramChannelAssignPayload {
  telegram_session_id: string;
  note?: string | null;
}

export interface AdminTelegramChannelOrphanPayload {
  note?: string | null;
}

export interface AdminMemeTemplateRead {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  is_curated: boolean;
  base_image_url: string | null;
  text_regions: Record<string, unknown>[] | null;
  created_at: string;
  updated_at: string;
}

export interface AdminBlockedPerceptualHashRead {
  id: string;
  perceptual_hash: string;
  hash_algorithm: string;
  hash_size: number;
  max_hamming_distance: number;
  reason: ModerationReason;
  note: string | null;
  is_active: boolean;
  created_by_admin_user_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface AdminBlockedPerceptualHashActionRead {
  action: 'deactivate' | 'delete';
  blocked_perceptual_hash_id: string;
  matched_meme_file_count: number;
  message: string;
}

export interface AdminBlockedPerceptualHashAuditRead {
  id: string;
  blocked_perceptual_hash_id: string;
  admin_user_id: string | null;
  action: string;
  previous_values: Record<string, unknown>;
  new_values: Record<string, unknown>;
  note: string | null;
  created_at: string;
}

export interface AdminMemeRead {
  id: string;
  media_type: ContentKind;
  language: ContentLanguage;
  is_nsfw: boolean;
  visibility_mode: 'auto' | 'force_public' | 'force_private';
  is_public: boolean;
  popularity_score: number;
  like_count: number;
  tags: string[];
  primary_file: PublicMemeFileRead | null;
  template_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface AdminMemeSeoPageRead {
  meme_id: string;
  slug: string;
  page_title: string;
  meta_description: string;
  alt_text: string;
  caption: string | null;
  body_text: string | null;
  tags: string[];
  model_id: string;
  prompt_version: string;
  generated_at: string;
  edited_at: string | null;
}

export interface AdminMemeSeoReviewRowRead {
  meme: AdminMemeRead;
  seo_page: AdminMemeSeoPageRead | null;
  status: 'missing' | 'generated' | 'edited';
}

export interface AdminModerationReportRead {
  id: string;
  meme_id: string;
  reporter_user_id: string | null;
  status: ModerationReportStatus;
  reason: ModerationReason;
  note: string | null;
  resolved_by_admin_user_id: string | null;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
  meme: AdminMemeRead;
}

export interface MemeReportRead {
  id: string;
  meme_id: string;
  status: ModerationReportStatus;
  reason: ModerationReason;
  note: string | null;
  created_at: string;
  updated_at: string;
}

export interface AdminModerationDecisionRead {
  id: string;
  meme_id: string;
  report_id: string | null;
  admin_user_id: string | null;
  action: ModerationAction;
  reason: ModerationReason | null;
  note: string | null;
  previous_is_public: boolean;
  previous_visibility_mode: 'auto' | 'force_public' | 'force_private';
  previous_is_nsfw: boolean;
  new_is_public: boolean;
  new_visibility_mode: 'auto' | 'force_public' | 'force_private';
  new_is_nsfw: boolean;
  previous_template_id: string | null;
  new_template_id: string | null;
  created_at: string;
}

export interface AdminMediaObservationRead {
  width?: number | null;
  height?: number | null;
  frame_rate?: number | string | null;
  duration_seconds?: number | null;
  bitrate_bps?: number | null;
  file_size_bytes?: number | null;
  video_codec?: string | null;
  audio_codec?: string | null;
  pixel_format?: string | null;
  video_profile?: string | null;
  video_level?: string | number | null;
}

export interface AdminMemeProcessingStageRead {
  stage: string;
  status: string;
  attempt_count: number;
  version: string;
  work_kind?: AdminRecoveryWorkKind;
  work_id?: string;
  safe_error?: string | null;
  normalized_reason?: string | null;
  actions?: AdminRecoveryActionCandidateRead[];
  active_job?: AdminRecoveryActiveJobRead | AdminRecoveryJobSummaryRead | null;
}

export interface AdminMemeProcessingFileRead {
  id: string;
  is_primary: boolean;
  status: string;
  mime_type?: string | null;
  width?: number | null;
  height?: number | null;
  file_size_bytes?: number | null;
  source_has_audio?: boolean | null;
  web_video_has_audio?: boolean | null;
  web_video_profile?: string | null;
  web_video_verified_at?: string | null;
  original?: AdminMediaObservationRead | null;
  output?: AdminMediaObservationRead | null;
  source_observation?: AdminMediaObservationRead | null;
  output_observation?: AdminMediaObservationRead | null;
  stages: AdminMemeProcessingStageRead[];
  actions?: AdminRecoveryActionCandidateRead[];
  version?: string;
  work_kind?: AdminRecoveryWorkKind;
  work_id?: string;
  active_job?: AdminRecoveryActiveJobRead | AdminRecoveryJobSummaryRead | null;
}

export interface AdminMemeDetailRead {
  meme: AdminMemeRead;
  reports: AdminModerationReportRead[];
  decisions: AdminModerationDecisionRead[];
  processing_files?: AdminMemeProcessingFileRead[];
}

export interface AdminMemeDestructiveActionRead {
  action: 'delete' | 'merge' | string;
  source_meme_id: string;
  target_meme_id: string | null;
  audit_log_id: string;
  affected_snapshot: Record<string, unknown>;
  message: string;
}

export interface AdminMemeTemplateActionRead {
  action: 'delete' | 'merge';
  source_template_id: string;
  target_template_id: string | null;
  affected_meme_count: number;
  message: string;
}
