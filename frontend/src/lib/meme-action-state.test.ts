import { get } from 'svelte/store';
import { describe, expect, it } from 'vitest';

import { createMemeActionState } from './meme-action-state';

describe('meme action state', () => {
  it('merges viewer action patches for repeated meme cards', () => {
    const state = createMemeActionState('viewer-1');

    state.publish('meme-1', { favorited: true, likeCount: 8 });
    state.publish('meme-1', { saved: true });
    state.publish('meme-1', { pinned: true });

    expect(get(state)).toEqual({
      'meme-1': {
        favorited: true,
        likeCount: 8,
        saved: true,
        pinned: true
      }
    });
  });

  it('preserves patches for the same viewer and clears them when the viewer changes', () => {
    const state = createMemeActionState('viewer-1');
    state.publish('meme-1', { favorited: true });

    state.syncViewer('viewer-1');
    expect(get(state)).toEqual({ 'meme-1': { favorited: true } });

    state.syncViewer('viewer-2');
    expect(get(state)).toEqual({});
  });
});
