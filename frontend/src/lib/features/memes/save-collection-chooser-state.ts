export interface SaveCollectionLoadToken {
  memeId: string;
  requestVersion: number;
  membershipKey: string;
}

export interface SaveCollectionMutationToken {
  memeId: string;
  requestVersion: number;
}

export function collectionMembershipKey(collectionIds: readonly string[] | undefined): string {
  if (collectionIds === undefined) return 'unknown';
  return JSON.stringify([...new Set(collectionIds)].sort());
}

export function createSaveCollectionRequestGate(initialMemeId: string | null = null) {
  let activeMemeId = initialMemeId;
  let loadVersion = 0;
  let mutationVersion = 0;

  return {
    reset(nextMemeId: string): boolean {
      if (nextMemeId === activeMemeId) return false;

      activeMemeId = nextMemeId;
      loadVersion += 1;
      mutationVersion += 1;
      return true;
    },

    beginLoad(memeId: string, collectionIds: readonly string[] | undefined): SaveCollectionLoadToken {
      return {
        memeId,
        requestVersion: ++loadVersion,
        membershipKey: collectionMembershipKey(collectionIds)
      };
    },

    isLatestLoad(token: SaveCollectionLoadToken, currentMemeId: string): boolean {
      return token.memeId === activeMemeId && currentMemeId === activeMemeId && token.requestVersion === loadVersion;
    },

    membershipChanged(token: SaveCollectionLoadToken, collectionIds: readonly string[] | undefined): boolean {
      return token.membershipKey !== collectionMembershipKey(collectionIds);
    },

    beginMutation(memeId: string): SaveCollectionMutationToken {
      // A mutation is newer than every load already in flight for this chooser.
      loadVersion += 1;
      return { memeId, requestVersion: ++mutationVersion };
    },

    isLatestMutation(token: SaveCollectionMutationToken, currentMemeId: string): boolean {
      return token.memeId === activeMemeId && currentMemeId === activeMemeId && token.requestVersion === mutationVersion;
    }
  };
}
