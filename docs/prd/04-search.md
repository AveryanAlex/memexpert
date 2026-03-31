# Search & Discovery

## How Search Works

Full-text search (OCR text, Russian morphology via Meilisearch) + semantic search (Voyage AI multimodal embeddings via Qdrant). Results ranked by: semantic relevance (0.4) + text relevance (0.3) + popularity (0.3). Weights tunable via A/B testing.

## Search Scope

Public memes + user's private memes (from collections). Merged, no visual distinction in bot. Subtly marked on web.

## Filters

In the website search sidebar, available at launch:

- **Tags/categories** — select one or more
- **NSFW** — show/hide (respects user's default from settings)
- **Media type** — image / GIF / video
- **Language** — Russian / English / any

## Trending

Computed from: growth rate of reposts across channels (new source appearances in 24–48h) + growth in platform engagement (sends, saves, views on MemeXpert). Both signals combined.

## "Meme of the Day"

Automatically selected: highest popularity growth over 24 hours. Displayed prominently on the home page. No manual override in V1 (could add admin override later).

---

## Recommendations

### Similar Memes

Embedding similarity. Shown on meme pages. All users.

### Personalized Feed

For all users with interaction history (including guests). Content-based using view/like/send history. Collaborative filtering planned as a later enhancement.

### Trending

Fastest growth in channel reposts + platform engagement. Default for users without history.
