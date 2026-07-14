import { get } from 'svelte/store';
import { describe, expect, it } from 'vitest';

import { createMemeActionState } from './meme-action-state';

describe('meme action state', () => {
  it('merges viewer action patches for repeated meme cards', () => {
    const state = createMemeActionState('viewer-1');

    state.publish('meme-1', { favorited: true, likeCount: 8 });
    state.publish('meme-1', { saved: true, savedCollectionIds: ['collection-1'] });
    state.publish('meme-1', { pinned: true });

    expect(get(state)).toEqual({
      'meme-1': {
        favorited: true,
        likeCount: 8,
        saved: true,
        savedCollectionIds: ['collection-1'],
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

  it('allows only one coordinated mutation for the same meme action', () => {
    const state = createMemeActionState('viewer-1');

    const operation = state.beginOperation('meme-1', 'favorite');
    expect(operation).not.toBeNull();
    expect(state.beginOperation('meme-1', 'favorite')).toBeNull();
    expect(get(state)).toEqual({ 'meme-1': { favoritePending: true } });

    expect(
      state.completeOperation(operation!, {
        favorited: true,
        likeCount: 8
      })
    ).toBe(true);
    expect(get(state)).toEqual({
      'meme-1': {
        favoritePending: false,
        favorited: true,
        likeCount: 8
      }
    });
    expect(state.beginOperation('meme-1', 'favorite')).not.toBeNull();
  });

  it('ignores an in-flight mutation after the viewer changes', () => {
    const state = createMemeActionState('viewer-1');
    const operation = state.beginOperation('meme-1', 'pin');
    expect(operation).not.toBeNull();

    state.syncViewer('viewer-2');

    expect(state.completeOperation(operation!, { pinned: true })).toBe(false);
    expect(get(state)).toEqual({});
  });
});
