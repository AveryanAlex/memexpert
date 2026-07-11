import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { AdminMemeRead, AdminModerationDecisionRead, AdminModerationReportRead, PublicMemeFileRead } from '$lib/api/types';
import ModerationPage from '../routes/admin/moderation/+page.svelte';
import MemeDetailPage from '../routes/admin/memes/[id]/+page.svelte';

describe('/admin/moderation page', () => {
  it('renders a media-first report queue and progressive specialist controls', () => {
    const image = meme({ id: 'meme-image', primary_file: file('image/jpeg', { preview_url: '/api/v1/media/files/image/preview' }) });
    const video = meme({ id: 'meme-video', media_type: 'video', primary_file: file('video/quicktime', { web_video_url: '/api/v1/media/files/video/web-video.mp4' }) });
    const audio = meme({ id: 'meme-audio', media_type: 'audio', primary_file: file('audio/mpeg', { original_url: '/api/v1/media/files/audio/original' }) });
    const reports = [report(image, 'nsfw'), report(video, 'spam'), report(audio, 'copyright')];
    const { body } = render(ModerationPage, {
      props: {
        data: { moderation: { reports, decisions: [decision(image.id)], memes: [image] }, loadError: null },
        form: null
      } as never
    });

    expect(body).toContain('Reports needing a decision');
    expect(body).toContain('Primary queue');
    expect(body).toContain('<img');
    expect(body).toContain('<video');
    expect(body).toContain('src="/api/v1/media/files/video/web-video.mp4" type="video/mp4"');
    expect(body).toContain('<audio');
    expect(body).toContain('2026-07-10 11:00 UTC');
    expect(body).toContain('Current state');
    expect(body).toContain('Reporter note');
    expect(body).toContain('Open full meme detail');
    expect(body).toContain('Resolution');
    expect(body).toContain('Decision note (optional)');
    expect(body).toContain('action="?/resolveModerationReport"');
    expect(body).toContain('action="?/updateMemeModeration"');
    expect(body).toContain('Recent decisions');
    expect(body).toContain('Meme metadata');
    expect(body).toContain('href="/admin/moderation/patterns"');
    expect(body).not.toContain('name="perceptual_hash"');
    expect(body).not.toMatch(/<details[^>]*\sopen(?:=|\s|>)/);
  });

  it('renders action and load errors as alerts', () => {
    const { body } = render(ModerationPage, {
      props: {
        data: { moderation: { reports: [], decisions: [], memes: [] }, loadError: 'Could not load moderation work.' },
        form: { message: 'Report is already closed.', error: true }
      } as never
    });
    expect(body.match(/role="alert"/g)).toHaveLength(2);
    expect(body).toContain('Report is already closed.');
    expect(body).toContain('Could not load moderation work.');
  });
});

describe('/admin/memes/[id] page', () => {
  it('puts preview, state, and open reports before overrides and collapses metadata, history, and danger actions', () => {
    const reviewedMeme = meme({ primary_file: file('image/jpeg', { preview_url: '/api/v1/media/files/detail/preview' }), is_public: false, is_nsfw: true });
    const { body } = render(MemeDetailPage, {
      props: {
        data: {
          detail: { meme: reviewedMeme, reports: [report(reviewedMeme, 'nsfw')], decisions: [decision(reviewedMeme.id)] },
          templates: [],
          loadError: null
        },
        form: null
      } as never
    });

    expect(body).toContain('/api/v1/media/files/detail/preview');
    expect(body).toContain('Catalog visibility');
    expect(body).toContain('Hidden');
    expect(body).toContain('Safety label');
    expect(body).toContain('Sensitive');
    expect(body.indexOf('Preview')).toBeLessThan(body.indexOf('Open reports'));
    expect(body.indexOf('Open reports')).toBeLessThan(body.indexOf('Overrides'));
    expect(body).toContain('Metadata');
    expect(body).toContain('Moderation history');
    expect(body).toContain('Danger zone');
    expect(body).toContain('Merge into another meme');
    expect(body).toContain('Delete permanently');
    expect(body).toContain('Type MERGE to confirm');
    expect(body).toContain('Type DELETE to confirm');
    expect(body.match(/name="confirmation_phrase"/g)).toHaveLength(2);
    expect(body).toContain('This cannot be undone.');
    expect(body).not.toContain('name="confirmation"');
    expect(body).not.toContain(reviewedMeme.id + ' to confirm');
    expect(body).not.toMatch(/<details[^>]*\sopen(?:=|\s|>)/);
  });

  it('renders meme action failures with an alert and danger styling', () => {
    const reviewedMeme = meme();
    const { body } = render(MemeDetailPage, {
      props: {
        data: { detail: { meme: reviewedMeme, reports: [], decisions: [] }, templates: [], loadError: null },
        form: { message: 'Type DELETE to confirm this action.', error: true }
      } as never
    });

    expect(body).toContain('role="alert"');
    expect(body).toContain('text-danger');
    expect(body).toContain('Type DELETE to confirm this action.');
  });
});

function meme(overrides: Partial<AdminMemeRead> = {}): AdminMemeRead {
  return {
    id: '33333333-3333-4333-8333-333333333333',
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    is_public: true,
    popularity_score: 4.2,
    like_count: 12,
    tags: ['reaction'],
    primary_file: null,
    template_id: null,
    author_user_id: null,
    created_at: '2026-07-10T10:00:00Z',
    updated_at: '2026-07-10T10:00:00Z',
    ...overrides
  };
}

function file(mimeType: string, renderOverrides: Partial<NonNullable<PublicMemeFileRead['render']>>): PublicMemeFileRead {
  return {
    id: `file-${mimeType}`,
    mime_type: mimeType,
    width: 800,
    height: 600,
    file_size_bytes: 1024,
    blur_hash: null,
    quality_score: 0.9,
    render: {
      thumbnail_url: null,
      preview_url: null,
      display_url: null,
      original_url: null,
      download_url: null,
      web_video_url: null,
      width: 800,
      height: 600,
      blur_hash: null,
      ...renderOverrides
    }
  };
}

function report(reportedMeme: AdminMemeRead, reason: AdminModerationReportRead['reason']): AdminModerationReportRead {
  return {
    id: `report-${reportedMeme.id}`,
    meme_id: reportedMeme.id,
    reporter_user_id: null,
    status: 'pending',
    reason,
    note: 'Please review this media.',
    resolved_by_admin_user_id: null,
    resolved_at: null,
    created_at: '2026-07-10T11:00:00Z',
    updated_at: '2026-07-10T11:00:00Z',
    meme: reportedMeme
  };
}

function decision(memeId: string): AdminModerationDecisionRead {
  return {
    id: `decision-${memeId}`,
    meme_id: memeId,
    report_id: null,
    admin_user_id: null,
    action: 'hide',
    reason: 'spam',
    note: 'Hidden after review.',
    previous_is_public: true,
    previous_is_nsfw: false,
    new_is_public: false,
    new_is_nsfw: false,
    previous_template_id: null,
    new_template_id: null,
    created_at: '2026-07-10T12:00:00Z'
  };
}
