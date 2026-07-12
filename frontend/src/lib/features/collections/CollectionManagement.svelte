<script lang="ts">
  import type { CurrentSessionRead, WebCollectionDetailRead } from '$lib/api/types';
  import {
    collectionInviteRows,
    collectionManagementNotice,
    collectionMemberRows
  } from '$lib/features/collections/view-model';
  import { ActionLink, Badge, Button, Card, Input, Select } from '$lib/ui';

  interface Props {
    detail: WebCollectionDetailRead;
    session: CurrentSessionRead | null;
    inviteUrl?: string;
  }

  let { detail, session, inviteUrl }: Props = $props();

  let copyMessage = $state<string | null>(null);

  const collection = $derived(detail.collection);
  const capabilities = $derived(detail.capabilities);
  const memberRows = $derived(collectionMemberRows(detail));
  const inviteRows = $derived(collectionInviteRows(collection.invites));
  const managementNotice = $derived(collectionManagementNotice(detail, session));

  async function copyInviteUrl(value: string) {
    copyMessage = null;
    try {
      await navigator.clipboard.writeText(value);
      copyMessage = 'Invite URL copied.';
    } catch {
      copyMessage = 'Could not copy automatically. Select the URL and copy it manually.';
    }
  }
</script>

<details class="my-7 rounded-xl border border-line bg-paper" open={Boolean(inviteUrl)}>
  <summary class="cursor-pointer px-5 py-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent">
    <span class="font-black">Manage collection</span>
    <span class="ml-2 text-sm text-muted">Rename, invitations, members, and deletion</span>
  </summary>

  <div class="border-t border-line p-4 sm:p-5">
    {#if inviteUrl}
      <Card class="mb-4 grid gap-2" aria-labelledby="invite-created-title">
        <h2 id="invite-created-title" class="m-0 text-2xl font-black tracking-[-0.04em]">Invite link ready</h2>
        <p class="m-0 text-muted">Share this link with a full MemeXpert account. It is shown once.</p>
        <div class="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
          <Input value={inviteUrl} readonly aria-label="Invite link" />
          <Button type="button" variant="secondary" onclick={() => copyInviteUrl(inviteUrl)}>Copy</Button>
        </div>
        {#if copyMessage}
          <p class="m-0 text-sm text-muted" role="status">{copyMessage}</p>
        {/if}
      </Card>
    {/if}

    <section class="grid gap-4 md:grid-cols-3" aria-label="Collection controls">
      {#if capabilities.can_rename}
        <Card class="grid gap-3 shadow-none">
          <form class="grid gap-3" method="POST" action="?/update">
            <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Collection details</h2>
            <Input name="title" value={collection.title} maxlength={120} required aria-label="Collection title" />
            <Input name="description" value={collection.description ?? ''} aria-label="Collection description" placeholder="Description" />
            <Select name="visibility" aria-label="Collection visibility">
              <option value="private" selected={collection.visibility === 'private'}>Private</option>
              <option value="unlisted" selected={collection.visibility === 'unlisted'}>Unlisted</option>
            </Select>
            <Button type="submit">Save changes</Button>
          </form>
        </Card>
      {/if}

      {#if capabilities.can_create_invites}
        <Card class="grid gap-3 shadow-none">
          <form class="grid gap-3" method="POST" action="?/createInvite">
            <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Invite link</h2>
            <Input name="label" placeholder="Optional label" aria-label="Invite label" />
            <Select name="role" aria-label="Invite role">
              <option value="viewer">Viewer</option>
              <option value="editor">Editor</option>
            </Select>
            <Input name="max_uses" type="number" min="1" value="1" aria-label="Maximum uses" />
            <Input name="expires_in_hours" type="number" min="1" max="720" value="168" aria-label="Expires in hours" />
            <Button type="submit">Create invite</Button>
          </form>
        </Card>
      {:else}
        <Card class="grid gap-3 shadow-none">
          <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Invite links</h2>
          <p class="m-0 text-muted">{managementNotice}</p>
          {#if session?.user.account_type === 'guest'}
            <ActionLink size="compact" href={`/account/telegram?returnTo=/collection/${collection.id}`}>Connect Telegram</ActionLink>
          {/if}
        </Card>
      {/if}

      {#if capabilities.can_delete}
        <form class="grid gap-3 rounded-[28px] border border-danger-line bg-danger-surface p-6" method="POST" action="?/delete">
          <h2 class="m-0 text-2xl font-black tracking-[-0.04em]">Danger zone</h2>
          <p class="m-0 text-muted">Delete this custom collection and remove its saved meme rows. The memes themselves stay in the catalog.</p>
          <Button variant="danger" type="submit">Delete collection</Button>
        </form>
      {/if}
    </section>

    <section class="mt-4 grid gap-4 md:grid-cols-2" aria-label="Members and invite state">
      <Card class="grid gap-3 shadow-none" aria-labelledby="members-title">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <h2 id="members-title" class="m-0 text-2xl font-black tracking-[-0.04em]">Members</h2>
          <Badge>{memberRows.length} total</Badge>
        </div>
        <p class="m-0 text-muted">{capabilities.can_manage_members ? 'Owners can update non-owner roles or remove non-owner members.' : managementNotice}</p>
        {#if memberRows.length > 0}
          <div class="grid gap-2">
            {#each memberRows as member (member.id)}
              <article class="grid gap-2 rounded-[20px] border border-line p-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                <div>
                  <p class="m-0 font-black">{member.label}</p>
                  <p class="m-0 text-sm text-muted">Joined {member.joined}</p>
                </div>
                <div class="grid gap-2 sm:justify-items-end">
                  <div class="flex flex-wrap gap-2 sm:justify-end">
                    <Badge>{member.role}</Badge>
                    {#if member.isOwner}<Badge tone="success">Owner</Badge>{/if}
                  </div>
                  {#if capabilities.can_manage_members && !member.isOwner}
                    <div class="flex flex-wrap gap-2 sm:justify-end">
                      <form class="flex flex-wrap gap-2" method="POST" action="?/updateMemberRole">
                        <input type="hidden" name="member_user_id" value={member.id} />
                        <Select class="min-w-[120px]" name="role" value={member.role} aria-label={`Role for ${member.label}`}>
                          <option value="viewer">Viewer</option>
                          <option value="editor">Editor</option>
                        </Select>
                        <Button size="compact" variant="secondary" type="submit">Update role</Button>
                      </form>
                      <form method="POST" action="?/removeMember">
                        <input type="hidden" name="member_user_id" value={member.id} />
                        <Button size="compact" variant="danger" type="submit">Remove</Button>
                      </form>
                    </div>
                  {:else if capabilities.can_manage_members && member.isOwner}
                    <p class="m-0 text-sm text-muted">Owner transfer and owner removal are not available here.</p>
                  {/if}
                </div>
              </article>
            {/each}
          </div>
        {:else}
          <p class="m-0 text-muted">No membership rows were returned for this collection.</p>
        {/if}
      </Card>

      <Card class="grid gap-3 shadow-none" aria-labelledby="invites-title">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <h2 id="invites-title" class="m-0 text-2xl font-black tracking-[-0.04em]">Invites</h2>
          <Badge>{inviteRows.length} total</Badge>
        </div>
        <p class="m-0 text-muted">{capabilities.can_revoke_invites ? 'Pending direct-link invites can be revoked. Accepted, revoked, and expired rows are final.' : managementNotice}</p>
        {#if inviteRows.length > 0}
          <div class="grid gap-2">
            {#each inviteRows as invite (invite.id)}
              <article class="grid gap-2 rounded-[20px] border border-line p-4">
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <p class="m-0 font-black">{invite.label}</p>
                  <div class="flex flex-wrap gap-2">
                    <Badge>{invite.role}</Badge>
                    <Badge>{invite.statusLabel}</Badge>
                  </div>
                </div>
                <div class="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                  <p class="m-0 text-sm text-muted">{invite.usage} · Expires {invite.expires} · Created {invite.created}</p>
                  {#if capabilities.can_revoke_invites && !invite.isTerminal}
                    <form method="POST" action="?/revokeInvite">
                      <input type="hidden" name="invite_id" value={invite.id} />
                      <Button size="compact" variant="danger" type="submit">Revoke</Button>
                    </form>
                  {:else if invite.isTerminal}
                    <p class="m-0 text-sm font-extrabold text-muted">No further action</p>
                  {/if}
                </div>
              </article>
            {/each}
          </div>
        {:else}
          <p class="m-0 text-muted">No invite rows yet. Create a direct invite link when the form is available.</p>
        {/if}
      </Card>
    </section>
  </div>
</details>
