export type ContentKind = 'audio' | 'gif' | 'image' | 'link' | 'text' | 'video';
export type ContentLanguage = 'en' | 'mixed' | 'none' | 'ru';
export type AccountType = 'full' | 'guest';

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
  viewer_has_favorited?: boolean | null;
  viewer_has_saved?: boolean | null;
  viewer_has_pinned?: boolean | null;
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

export interface PublicMemeLandingRead {
  kind: 'tag' | 'template' | string;
  slug: string;
  title: string;
  description: string | null;
  page: PublicMemeSearchPageRead;
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
  created_at: string;
}
