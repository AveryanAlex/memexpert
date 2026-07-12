import type { CollectionInviteRead, CollectionInviteStatus, CollectionMembershipRole, CurrentSessionRead, WebCollectionDetailRead } from '$lib/api/types';

export interface CollectionMemberView {
  id: string;
  role: CollectionMembershipRole;
  joined: string;
  isOwner: boolean;
  label: string;
}

export interface CollectionInviteView {
  id: string;
  label: string;
  role: CollectionMembershipRole;
  status: CollectionInviteStatus;
  statusLabel: string;
  isTerminal: boolean;
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

export function collectionInviteRows(invites: CollectionInviteRead[], now = new Date()): CollectionInviteView[] {
  return invites.map((invite) => ({
    id: invite.id,
    label: invite.label?.trim() || 'Direct invite link',
    role: invite.role,
    status: invite.status,
    statusLabel: inviteStatusLabel(invite, now),
    isTerminal: invite.status !== 'pending' || isExpired(invite, now),
    usage: invite.max_uses === null ? `${invite.use_count} used` : `${invite.use_count}/${invite.max_uses} used`,
    expires: invite.expires_at ? formatDate(invite.expires_at) : 'No expiry',
    created: formatDate(invite.created_at)
  }));
}

export function collectionManagementNotice(detail: WebCollectionDetailRead, session: CurrentSessionRead | null): string {
  if (detail.capabilities.can_manage_members && detail.capabilities.can_create_invites) {
    return 'Owners can update collection details, create or revoke invite links, and manage non-owner members.';
  }

  if (detail.capabilities.can_manage_members) {
    return 'Owners can update collection details, revoke pending invite links, and manage non-owner members. Creating invites requires a connected collaboration identity.';
  }

  if (detail.capabilities.can_create_invites && detail.capabilities.can_revoke_invites) {
    return 'Editors can create and revoke invite links. Member role changes and removals require owner access.';
  }

  if (detail.capabilities.can_revoke_invites) {
    return 'Editors can revoke pending invite links. Creating invites requires a connected collaboration identity, and member changes require owner access.';
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
  return `${new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeZone: 'UTC' }).format(new Date(value))} UTC`;
}

function inviteStatusLabel(invite: CollectionInviteRead, now: Date): string {
  if (invite.status === 'pending' && isExpired(invite, now)) {
    return 'expired';
  }

  return invite.status;
}

function isExpired(invite: CollectionInviteRead, now: Date): boolean {
  return invite.expires_at !== null && new Date(invite.expires_at).getTime() <= now.getTime();
}
