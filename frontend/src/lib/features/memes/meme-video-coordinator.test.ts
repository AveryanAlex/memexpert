import { describe, expect, it, vi } from 'vitest';

import { createMemeVideoCoordinator } from './meme-video-coordinator';

describe('meme video coordinator', () => {
  it('keeps playback and unmuted audio exclusive', () => {
    const coordinator = createMemeVideoCoordinator();
    const first = { pauseAndMute: vi.fn(), mute: vi.fn(), startViewportAutoplay: vi.fn() };
    const second = { pauseAndMute: vi.fn(), mute: vi.fn(), startViewportAutoplay: vi.fn() };
    coordinator.register('first', first);
    coordinator.register('second', second);

    coordinator.activatePlayback('first');
    coordinator.activatePlayback('second');
    coordinator.activateAudio('first');
    coordinator.activateAudio('second');

    expect(first.pauseAndMute).toHaveBeenCalledOnce();
    expect(first.mute).toHaveBeenCalledOnce();
    expect(second.pauseAndMute).not.toHaveBeenCalled();
    expect(second.mute).not.toHaveBeenCalled();
  });

  it('arbitrates multiple viewport-autoplay candidates without playback ping-pong', () => {
    const coordinator = createMemeVideoCoordinator();
    const first = { pauseAndMute: vi.fn(), mute: vi.fn(), startViewportAutoplay: vi.fn() };
    const second = { pauseAndMute: vi.fn(), mute: vi.fn(), startViewportAutoplay: vi.fn() };
    coordinator.register('first', first);
    coordinator.register('second', second);

    coordinator.setViewportAutoplayEligible('first', true);
    coordinator.setViewportAutoplayEligible('second', true);

    expect(first.startViewportAutoplay).toHaveBeenCalledOnce();
    expect(second.startViewportAutoplay).not.toHaveBeenCalled();

    coordinator.setViewportAutoplayEligible('first', false);

    expect(first.startViewportAutoplay).toHaveBeenCalledOnce();
    expect(second.startViewportAutoplay).toHaveBeenCalledOnce();
  });

  it('resumes an eligible viewport candidate after an explicit preview releases playback', () => {
    const coordinator = createMemeVideoCoordinator();
    const viewport = { pauseAndMute: vi.fn(), mute: vi.fn(), startViewportAutoplay: vi.fn() };
    const hovered = { pauseAndMute: vi.fn(), mute: vi.fn(), startViewportAutoplay: vi.fn() };
    coordinator.register('viewport', viewport);
    coordinator.register('hovered', hovered);
    coordinator.setViewportAutoplayEligible('viewport', true);

    coordinator.activatePlayback('hovered');
    coordinator.release('hovered');

    expect(viewport.pauseAndMute).toHaveBeenCalledOnce();
    expect(viewport.startViewportAutoplay).toHaveBeenCalledTimes(2);
  });
});
