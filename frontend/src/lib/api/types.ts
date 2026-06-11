export type ContentKind = 'audio' | 'gif' | 'image' | 'link' | 'text' | 'video';
export type ContentLanguage = 'en' | 'mixed' | 'none' | 'ru';
export type SourcePlatform = 'reddit' | 'telegram' | 'vk';
export type ChannelSuggestionStatus = 'approved' | 'pending' | 'rejected';

export interface PublicMemeFileRead {
  id: string;
  mime_type: string | null;
  width: number | null;
  height: number | null;
  file_size_bytes: number | null;
  blur_hash: string | null;
  quality_score: number;
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

export interface UserRead {
  id: string;
  account_type: 'guest' | 'full';
  email: string | null;
  telegram_id: number | null;
  google_id: string | null;
  is_admin: boolean;
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
