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
export type AdminSourceBackfillStatus = 'failed' | 'idle' | 'queued' | 'running';
export type AdminSourcePostIndexStatus = 'failed' | 'indexed' | 'not_indexable' | 'partially_indexed' | 'processing';
export type AdminSourcePostSyncStatus = 'failed' | 'pending' | 'processing' | 'synced';
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

export interface MemeResultAttributionRead {
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
}

export interface AdminSourcePostRead {
  id: string;
  post_id: string;
  telegram_url: string | null;
  published_at: string | null;
  observed_at: string;
  media_type: string | null;
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

export interface AdminMemeDetailRead {
  meme: AdminMemeRead;
  reports: AdminModerationReportRead[];
  decisions: AdminModerationDecisionRead[];
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
