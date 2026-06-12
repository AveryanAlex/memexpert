export type ContentKind = 'audio' | 'gif' | 'image' | 'link' | 'text' | 'video';
export type ContentLanguage = 'en' | 'mixed' | 'none' | 'ru';
export type AccountType = 'full' | 'guest';
export type CollectionKind = 'custom' | 'favorites';
export type CollectionVisibility = 'private' | 'public' | 'unlisted';
export type CollectionMembershipRole = 'editor' | 'owner' | 'viewer';
export type CollectionInviteChannel = 'direct_link' | 'email' | 'telegram';
export type CollectionInviteStatus = 'accepted' | 'expired' | 'pending' | 'revoked';

export interface UserRead {
  id: string;
  account_type: AccountType;
  telegram_id: number | null;
  google_id: string | null;
  email: string | null;
  email_verified_at: string | null;
  language: ContentLanguage | 'any';
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

export interface TelegramLinkStartRead {
  code: string;
  deep_link_url: string;
  expires_at: string;
  expires_in_seconds: number;
  return_url: string;
}
export type SourcePlatform = 'reddit' | 'telegram' | 'vk';
export type ChannelSuggestionStatus = 'approved' | 'pending' | 'rejected';
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

export interface PublicMemeSearchResultRead {
  meme: PublicMemeCardRead;
}

export interface PublicMemeSearchPageRead {
  items: PublicMemeSearchResultRead[];
  limit: number;
  offset: number;
  total: number;
  has_more: boolean;
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
}

export interface PublicMemeTrendPageRead {
  items: PublicMemeTrendRead[];
  limit: number;
  offset: number;
  total: number;
  has_more: boolean;
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

export interface PublicTrendSummaryRead {
  kind: 'tag' | 'template' | string;
  slug: string;
  title: string;
  description: string | null;
  meme_count: number;
  trend: PublicTrendMetricsRead;
}

export interface CollectionCapabilitiesRead {
  can_view: boolean;
  can_add_memes: boolean;
  can_remove_memes: boolean;
  can_rename: boolean;
  can_delete: boolean;
  can_create_invites: boolean;
  can_set_active_save: boolean;
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

export interface AdminSessionRead {
  user: UserRead;
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
  catchup_message_limit: number;
  session_id: string | null;
  last_read_post_id: string | null;
  last_fetched_at: string | null;
  operational_status: 'active' | 'inactive' | 'paused';
  freshness_status: 'checkpoint_only' | 'fresh' | 'never_fetched' | 'stale';
  seconds_since_last_fetch: number | null;
  created_at: string;
  updated_at: string;
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
  is_public: boolean;
  popularity_score: number;
  like_count: number;
  tags: string[];
  template_id: string | null;
  author_user_id: string | null;
  created_at: string;
  updated_at: string;
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
  previous_is_nsfw: boolean;
  new_is_public: boolean;
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
