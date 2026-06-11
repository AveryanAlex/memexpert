export type ContentKind = 'audio' | 'gif' | 'image' | 'link' | 'text' | 'video';
export type ContentLanguage = 'en' | 'mixed' | 'none' | 'ru';

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
  created_at: string;
  updated_at: string;
}

export interface PublicMemeDetailRead extends PublicMemeCardRead {
  ocr_text: string | null;
  seo_page_slug: string | null;
  seo_title: string | null;
  seo_description: string | null;
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
