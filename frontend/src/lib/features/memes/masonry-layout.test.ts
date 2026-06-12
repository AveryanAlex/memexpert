import { describe, expect, it } from "vitest";

import type { PublicMemeCardRead } from "$lib/api/types";
import {
  buildMasonryColumns,
  masonryColumnCount,
  masonryColumnWidth,
} from "./masonry-layout";

describe("masonryColumnCount", () => {
  it("uses container width instead of Tailwind breakpoints", () => {
    expect(masonryColumnCount(0)).toBe(1);
    expect(masonryColumnCount(279)).toBe(1);
    expect(masonryColumnCount(576)).toBe(2);
    expect(masonryColumnCount(880)).toBe(3);
    expect(masonryColumnCount(1600)).toBe(4);
  });
});

describe("masonryColumnWidth", () => {
  it("matches the rendered flex column width estimate", () => {
    expect(masonryColumnWidth(880, 3)).toBeCloseTo((880 - 16 * 2) / 3);
    expect(masonryColumnWidth(0, 3)).toBe(280);
  });
});

describe("buildMasonryColumns", () => {
  it("places backend-ordered items into the current shortest column deterministically", () => {
    const memes = [
      memeCard("rank-1", 500, 1200),
      memeCard("rank-2", 1000, 700),
      memeCard("rank-3", 800, 400),
      memeCard("rank-4", 500, 500),
      memeCard("rank-5", 600, 1400),
    ];

    const first = buildMasonryColumns(memes, 2).map((column) =>
      column.items.map((meme) => meme.id),
    );
    const second = buildMasonryColumns(memes, 2).map((column) =>
      column.items.map((meme) => meme.id),
    );

    expect(first).toEqual(second);
    expect(first).toEqual([
      ["rank-1", "rank-4"],
      ["rank-2", "rank-3", "rank-5"],
    ]);
    expect(first.flat().sort()).toEqual(memes.map((meme) => meme.id).sort());
  });

  it("preserves exact backend order when rendered as one mobile column", () => {
    const memes = [
      memeCard("rank-1", 300, 900),
      memeCard("rank-2", 1200, 600),
      memeCard("rank-3", null, null),
    ];

    expect(
      buildMasonryColumns(memes, 1)[0].items.map((meme) => meme.id),
    ).toEqual(["rank-1", "rank-2", "rank-3"]);
  });
});

function memeCard(
  id: string,
  width: number | null,
  height: number | null,
): PublicMemeCardRead {
  return {
    id,
    media_type: "image",
    language: "en",
    is_nsfw: false,
    popularity_score: 10,
    like_count: 1,
    tags: ["test"],
    primary_file:
      width && height
        ? {
            id: `${id}-file`,
            mime_type: "image/jpeg",
            width,
            height,
            file_size_bytes: 1000,
            blur_hash: null,
            quality_score: 1,
            render: {
              thumbnail_url: null,
              preview_url: null,
              display_url: `https://cdn.example.test/${id}.jpg`,
              original_url: null,
              download_url: null,
              web_video_url: null,
              width,
              height,
              blur_hash: null,
            },
          }
        : null,
    caption: `Caption for ${id}`,
    seo_page_slug: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false,
  };
}
