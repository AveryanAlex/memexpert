# Collections & User-Generated Content

## Collections

Every account (including guest) has **Favorites** (auto-created, not deletable). Full accounts can create additional collections.

## Roles

Owner (full control), Editor (add/remove memes, invite viewers), Viewer (read-only).

## Sharing

Via invite links. Shared as Mini App deep links in Telegram: `t.me/MemeXpertBot/app?startapp=invite_XXXXX`. No public collections at launch.

## Pins

Up to 20. Shown first in inline empty query. Full accounts only.

## Like = Favorite

Liking a meme adds it to the Favorites collection. The total like count (number of unique users who favorited) is displayed publicly on the meme page. This count also feeds into popularity scoring and trending calculations.

## User Uploads

Users upload memes to collections. Uploads are:

- **Deduplicated against public database** — if match found, the existing public meme is added to the collection instead (user notified)
- **Deduplicated within target collection** — no duplicate entries
- **Not cross-user deduplicated** — separate entities for different users' private uploads
- Processed through transcoding + OCR + embedding pipeline
- No SEO pages generated
- Visible only to uploader and collection members
- Searchable by uploader in inline
- Deleted from storage when removed from all collections

## Moderation

Private collections: not auto-moderated. "Report" button for members. Admin manual review.
