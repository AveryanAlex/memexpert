import type { CollectionInviteRead, CurrentSessionRead, WebCollectionDetailRead } from '$lib/api/types';

export interface CollectionMemberView {
  id: string;
  role: string;
  joined: string;
  isOwner: boolean;
  label: string;
}

export interface CollectionInviteView {
  id: string;
  label: string;
  role: string;
  status: string;
  usage: string;
  expires: string;
  created: string;
}

export function collectionMemberRows(detail: WebCollectionDetailRead): CollectionMemberView[] {
  return detail.collection.memberships.map((membership) => ({
    id: membership.user_id,
    role: membership.role,
    joined: formatDate(membership.joined_at),
    isOwner: membership.user_id === detail.collection.owner_id,
    label: shortId(membership.user_id)
  }));
}

export function collectionInviteRows(invites: CollectionInviteRead[]): CollectionInviteView[] {
  return invites.map((invite) => ({
    id: invite.id,
    label: invite.label?.trim() || 'Direct invite link',
    role: invite.role,
    status: invite.status,
    usage: invite.max_uses === null ? `${invite.use_count} used` : `${invite.use_count}/${invite.max_uses} used`,
    expires: invite.expires_at ? formatDate(invite.expires_at) : 'No expiry',
    created: formatDate(invite.created_at)
  }));
}

export function collectionManagementNotice(detail: WebCollectionDetailRead, session: CurrentSessionRead | null): string {
  if (detail.capabilities.can_create_invites) {
    return 'Create direct invite links here. Existing member removal, invite revocation, and role changes are not exposed by the current API.';
  }

  if (session?.user.account_type === 'guest') {
    return 'Connect Telegram to use full-account collaboration actions such as creating invite links.';
  }

  if (!detail.capabilities.can_add_memes) {
    return 'You can view this collection, but member and invite management require editor or owner access.';
  }

  return 'This collection can be edited, but invite creation requires a connected collaboration identity.';
}

export function collectionAccessSummary(detail: WebCollectionDetailRead): string {
  const collection = detail.collection;
  if (collection.kind === 'favorites') {
    return 'Favorites is the default private collection for this account session.';
  }

  return `${collection.visibility} custom collection. ${detail.capabilities.can_add_memes ? 'Editors and owners can add memes.' : 'Your access is view-only.'}`;
}

function shortId(value: string): string {
  return value.length <= 12 ? value : `${value.slice(0, 8)}...${value.slice(-4)}`;
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat('en', { dateStyle: 'medium' }).format(new Date(value));
}
