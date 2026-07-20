import { expect, test } from '../fixtures/app';

test('fake-provider upload becomes public and searchable through the website', async ({ api, app, seed }) => {
  const created = seed.created_meme;

  await api.expectHealthy();
  await api.expectDualIndexProof(created.meme_file_id);
  await api.expectMemeInSearch(created.meme_id, { query: created.query, limit: '10', offset: '0' });
  await api.expectMemeDetail(created.slug, created.meme_id);

  await app.home.goto();
  await app.home.searchFor(created.query);
  await app.search.expectResultVisible(created);
  await app.search.openResult(created);
  await app.detail.expectOpen(created);
});

test('real audible and silent media downloads satisfy the audio-safe web profile', async ({ seed }) => {
  expect(seed.audio_safe_media).toHaveLength(2);
  const audible = seed.audio_safe_media.find((item) => item.fixture === 'audible-webm-opus');
  const silent = seed.audio_safe_media.find((item) => item.fixture === 'silent-webm-60fps');

  expect(audible).toEqual(
    expect.objectContaining({
      profile: 'web-h264-aac-1080p30-v2',
      frame_rate: 24,
      video_codec: 'h264',
      audio_codec: 'aac',
      source_has_audio: true,
      web_video_has_audio: true
    })
  );
  expect(silent).toEqual(
    expect.objectContaining({
      profile: 'web-h264-aac-1080p30-v2',
      frame_rate: 30,
      video_codec: 'h264',
      audio_codec: null,
      source_has_audio: false,
      web_video_has_audio: false
    })
  );
  for (const proof of seed.audio_safe_media) {
    expect(proof.downloaded_byte_size).toBeGreaterThan(0);
    expect(proof.poster_byte_size).toBeGreaterThan(0);
    expect(proof.video_bit_rate).toBeGreaterThan(0);
    expect(proof.video_bit_rate).toBeLessThanOrEqual(6_300_000);
    expect(proof.frame_rate).toBeLessThanOrEqual(30.01);
    expect(proof.width % 2).toBe(0);
    expect(proof.height % 2).toBe(0);
  }
});
