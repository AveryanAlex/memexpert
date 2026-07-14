import { describe, expect, it } from 'vitest';
import { collectionMembershipKey, createSaveCollectionRequestGate } from './save-collection-chooser-state';

describe('save collection chooser request gate', () => {
  it('rejects a superseded load result', () => {
    const gate = createSaveCollectionRequestGate('meme-one');
    const stale = gate.beginLoad('meme-one', []);
    const current = gate.beginLoad('meme-one', []);

    expect(gate.isLatestLoad(stale, 'meme-one')).toBe(false);
    expect(gate.isLatestLoad(current, 'meme-one')).toBe(true);
  });

  it('rejects old load and mutation results after the component switches memes', () => {
    const gate = createSaveCollectionRequestGate('meme-one');
    const staleLoad = gate.beginLoad('meme-one', []);
    const staleMutation = gate.beginMutation('meme-one');

    expect(gate.reset('meme-two')).toBe(true);
    expect(gate.isLatestLoad(staleLoad, 'meme-two')).toBe(false);
    expect(gate.isLatestMutation(staleMutation, 'meme-two')).toBe(false);

    const currentLoad = gate.beginLoad('meme-two', []);
    expect(gate.isLatestLoad(currentLoad, 'meme-two')).toBe(true);
  });

  it('makes a mutation newer than an already-running collection load', () => {
    const gate = createSaveCollectionRequestGate('meme-one');
    const staleLoad = gate.beginLoad('meme-one', []);
    const mutation = gate.beginMutation('meme-one');

    expect(gate.isLatestLoad(staleLoad, 'meme-one')).toBe(false);
    expect(gate.isLatestMutation(mutation, 'meme-one')).toBe(true);
  });

  it('detects membership changes without depending on collection ordering or duplicates', () => {
    const gate = createSaveCollectionRequestGate('meme-one');
    const load = gate.beginLoad('meme-one', ['collection-b', 'collection-a']);

    expect(collectionMembershipKey(['collection-a', 'collection-b', 'collection-a'])).toBe(
      collectionMembershipKey(['collection-b', 'collection-a'])
    );
    expect(gate.membershipChanged(load, ['collection-a', 'collection-b'])).toBe(false);
    expect(gate.membershipChanged(load, ['collection-a', 'collection-c'])).toBe(true);
  });
});
