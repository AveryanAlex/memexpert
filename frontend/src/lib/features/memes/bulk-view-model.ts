import { memeDownloadUrl, memeRenderUrl, memeTitle } from '$lib/memeActions';
import type { AccountType, CollectionSummaryRead, PublicMemeCardRead, WebCollectionListRead } from '$lib/api/types';

export interface BulkCollectionOption {
  id: string;
  title: string;
  kind: CollectionSummaryRead['kind'];
  can_write: boolean;
}

export interface MemeGridBulkOptions {
  enabled: boolean;
  accountType?: AccountType | null;
  saveEnabled?: boolean;
  collectionOptions?: BulkCollectionOption[];
  removeCollectionId?: string | null;
  removeEnabled?: boolean;
  guidance?: string | null;
}

export interface BulkDownloadItem {
  id: string;
  title: string;
  url: string;
}

export function selectedMemes(memes: PublicMemeCardRead[], selectedIds: string[]): PublicMemeCardRead[] {
  const selected = new Set(selectedIds);
  return memes.filter((meme) => selected.has(meme.id));
}

export function bulkDownloadItems(memes: PublicMemeCardRead[]): BulkDownloadItem[] {
  return memes.flatMap((meme) => {
    const url = memeDownloadUrl(meme) ?? memeRenderUrl(meme);
    return url ? [{ id: meme.id, title: memeTitle(meme), url }] : [];
  });
}

export function bulkToolbarSummary(total: number, selected: number, downloadable: number): string {
  if (selected === 0) {
    return `${total} meme${total === 1 ? '' : 's'} available for selection.`;
  }

  return `${selected} selected. ${downloadable} ${downloadable === 1 ? 'has' : 'have'} a media URL for download.`;
}

export function bulkGuestGuidance(accountType: AccountType | null | undefined, hasCustomCollections: boolean): string | null {
  if (accountType === 'guest' && !hasCustomCollections) {
    return 'Guests can bulk-save into Favorites. Connect Telegram for custom collections, uploads, and member actions.';
  }

  return null;
}

export function bulkCollectionOptions(collections: CollectionSummaryRead[] | null | undefined): BulkCollectionOption[] {
  return (collections ?? [])
    .filter((collection) => collection.can_write)
    .map((collection) => ({
      id: collection.id,
      title: collection.title,
      kind: collection.kind,
      can_write: collection.can_write
    }));
}

export function collectionListBulkOptions(collections: WebCollectionListRead | null | undefined): BulkCollectionOption[] {
  return (collections?.collections ?? [])
    .filter((item) => item.capabilities.can_add_memes)
    .map((item) => ({
      id: item.collection.id,
      title: item.collection.title,
      kind: item.collection.kind,
      can_write: item.capabilities.can_add_memes
    }));
}
