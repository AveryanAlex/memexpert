<script lang="ts">
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { ActionLink, Badge, Button, EmptyState, FormRow, Input, Notice, Select, Textarea } from '$lib/ui';
  import type { AdminSourceChannelRead, AdminTelegramChannelGroupRead, AdminTelegramSessionRead, TelegramSessionStatus } from '$lib/api/types';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

  const sessionStatuses: TelegramSessionStatus[] = ['active', 'auth_required', 'flood_wait', 'quarantined', 'stopped'];
  const channels = $derived(data.telegramAdmin.groups.flatMap((group) => group.channels));

  function sessionLabel(session: AdminTelegramSessionRead): string {
    return session.display_name === session.name ? session.name : `${session.display_name} (${session.name})`;
  }

  function groupTitle(group: AdminTelegramChannelGroupRead): string {
    if (group.is_orphaned) {
      return `Orphaned Telegram channels (${group.channels.length})`;
    }
    if (!group.telegram_session) {
      return `Unassigned Telegram channels (${group.channels.length})`;
    }
    return `${sessionLabel(group.telegram_session)} (${group.channels.length})`;
  }

  function channelLabel(channel: AdminSourceChannelRead): string {
    return `${channel.title} · ${channel.platform_id}${channel.username ? ` · @${channel.username}` : ''}`;
  }

  function freshnessCopy(channel: AdminSourceChannelRead): string {
    if (channel.freshness_status === 'fresh') {
      return `fresh${channel.seconds_since_last_fetch === null ? '' : ` · ${formatAge(channel.seconds_since_last_fetch)} ago`}`;
    }
    if (channel.freshness_status === 'stale') {
      return `stale · ${formatAge(channel.seconds_since_last_fetch ?? 0)} ago`;
    }
    if (channel.freshness_status === 'checkpoint_only') {
      return `checkpoint only · last post ${channel.last_read_post_id}`;
    }
    return 'never fetched';
  }

  function formatAge(seconds: number): string {
    if (seconds >= 86400) return `${Math.floor(seconds / 86400)}d`;
    if (seconds >= 3600) return `${Math.floor(seconds / 3600)}h`;
    if (seconds >= 60) return `${Math.floor(seconds / 60)}m`;
    return `${seconds}s`;
  }

  function formatTimestamp(value: string | null): string {
    if (!value) return 'none';
    return value.replace('T', ' ').replace(/\.\d+(Z|[+-]\d\d:\d\d)?$/, '$1');
  }

  function accountCopy(session: AdminTelegramSessionRead): string {
    const parts = [
      session.account_username ? `@${session.account_username}` : null,
      session.account_user_id === null ? null : `user ${session.account_user_id}`,
      session.account_phone_hint ? `phone ${session.account_phone_hint}` : null
    ].filter(Boolean);
    return parts.length > 0 ? parts.join(' · ') : 'no account metadata';
  }
</script>

<section>
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div>
      <Badge>Signed in as {data.adminUser.email || data.adminUser.id}</Badge>
      <h1 class="my-3 text-[clamp(2.4rem,8vw,5rem)] font-black leading-[0.9] tracking-[-0.075em]">Telegram admin</h1>
      <p class="m-0 max-w-3xl text-muted">
        Manage DB-backed Telegram sessions and source-channel assignment without exposing Telegram login secrets.
      </p>
    </div>
    <ActionLink href="/admin" variant="secondary">Back to admin tools</ActionLink>
  </div>
</section>

{#if form?.message}
  <Notice tone={form.error ? 'danger' : 'success'} role={form.error ? 'alert' : 'status'}>{form.message}</Notice>
{/if}

{#if data.loadError}
  <Notice role="alert" tone="danger">{data.loadError}</Notice>
{/if}

<div class="my-5 grid gap-4 xl:grid-cols-[minmax(340px,0.75fr)_minmax(0,1.25fr)]">
  <AdminPanel title="Create Session">
    <p class="m-0 text-sm text-muted">Create the crawler policy row first, then authenticate it from the session card with Telegram QR or phone-code login.</p>
    <form method="POST" action="?/createSession" class="grid gap-3">
      <div class="grid gap-3 md:grid-cols-2">
        <FormRow label="Name" hint="Stable operator-facing key."><Input name="name" maxlength={64} placeholder="primary_ingest" required /></FormRow>
        <FormRow label="Display name"><Input name="display_name" placeholder="Primary ingest account" /></FormRow>
      </div>
      <div class="grid gap-3 md:grid-cols-2">
        <FormRow label="Max requests/sec"><Input name="max_requests_per_second" type="number" min="0.1" step="0.1" value="1" required /></FormRow>
        <FormRow label="Audit note"><Input name="note" placeholder="why this session is being added" /></FormRow>
      </div>
      <div class="grid gap-2 rounded-2xl border border-line bg-soft/40 p-3 md:grid-cols-2">
        <label class="inline-flex items-center gap-2 text-chiptext"><input name="enabled" type="checkbox" checked /> Enabled</label>
        <label class="inline-flex items-center gap-2 text-chiptext"><input name="catchup_enabled" type="checkbox" checked /> Catch-up enabled</label>
        <label class="inline-flex items-center gap-2 text-chiptext"><input name="live_enabled" type="checkbox" checked /> Live enabled</label>
        <label class="inline-flex items-center gap-2 text-chiptext"><input name="engagement_enabled" type="checkbox" checked /> Engagement enabled</label>
      </div>
      <Button type="submit">Create session</Button>
    </form>
  </AdminPanel>

  <AdminPanel title="Add Telegram Channel">
    <p class="m-0 text-sm text-muted">Assign new Telegram channels to a DB-backed session or intentionally create them orphaned. Orphaned channels are non-indexable and crawler controls are forced off.</p>
    <form method="POST" action="?/addChannel" class="grid gap-3">
      <div class="grid gap-3 md:grid-cols-2">
        <FormRow label="Platform id"><Input name="platform_id" placeholder="-1001234567890 or public handle" required /></FormRow>
        <FormRow label="Title"><Input name="title" placeholder="Source title" required /></FormRow>
      </div>
      <div class="grid gap-3 md:grid-cols-3">
        <FormRow label="Username"><Input name="username" placeholder="optional" /></FormRow>
        <FormRow label="Subscribers"><Input name="subscriber_count" type="number" min="0" /></FormRow>
        <FormRow label="Catch-up limit"><Input name="catchup_message_limit" type="number" min="1" max="10000" value="500" required /></FormRow>
      </div>
      <FormRow label="Assignment" hint="Choose Orphaned when no session should index this source yet.">
        <Select name="assignment" required>
          <option value="orphaned">Orphaned, non-indexable</option>
          {#each data.telegramAdmin.sessions as session (session.id)}
            <option value={session.id}>{sessionLabel(session)}</option>
          {/each}
        </Select>
      </FormRow>
      <div class="grid gap-2 rounded-2xl border border-line bg-soft/40 p-3 md:grid-cols-3">
        <label class="inline-flex items-center gap-2 text-chiptext"><input name="catchup_enabled" type="checkbox" checked /> Catch-up enabled when assigned</label>
        <label class="inline-flex items-center gap-2 text-chiptext"><input name="live_enabled" type="checkbox" checked /> Live enabled when assigned</label>
        <label class="inline-flex items-center gap-2 text-chiptext"><input name="engagement_enabled" type="checkbox" checked /> Engagement enabled when assigned</label>
      </div>
      <Button type="submit">Add Telegram channel</Button>
    </form>
  </AdminPanel>
</div>

<AdminPanel title="Sessions">
  {#if data.telegramAdmin.sessions.length === 0}
    <EmptyState title="No Telegram sessions" message="Create and authenticate a session before assigning indexable Telegram channels." />
  {:else}
    <div class="grid gap-4">
      {#each data.telegramAdmin.sessions as session (session.id)}
        <article class="grid gap-4 rounded-3xl border border-line bg-paper p-4">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 class="m-0 text-xl font-black tracking-[-0.03em]">{session.display_name}</h3>
              <p class="m-0 text-sm text-muted">{session.name} · ID {session.id}</p>
            </div>
            <div class="flex flex-wrap gap-2">
              <Badge tone={session.status === 'active' ? 'success' : 'neutral'}>{session.status}</Badge>
              <Badge class={session.enabled ? '' : 'border-danger-line bg-danger-surface text-danger'}>{session.enabled ? 'enabled' : 'disabled'}</Badge>
              <Badge>{session.owned_channel_count} channels</Badge>
              <Badge>{session.has_string_session ? 'session key stored' : 'login required'}</Badge>
            </div>
          </div>

          <div class="grid gap-2 text-sm md:grid-cols-2 xl:grid-cols-4">
            <p class="m-0"><strong>Policy:</strong> catch-up {session.catchup_enabled ? 'on' : 'off'} · live {session.live_enabled ? 'on' : 'off'} · engagement {session.engagement_enabled ? 'on' : 'off'} · {session.max_requests_per_second}/sec</p>
            <p class="m-0"><strong>Heartbeat:</strong> {formatTimestamp(session.last_heartbeat_at)} · listener {formatTimestamp(session.live_listener_started_at)}</p>
            <p class="m-0"><strong>Health:</strong> flood wait {formatTimestamp(session.flood_wait_until)} · quarantined {formatTimestamp(session.quarantined_at)}</p>
            <p class="m-0"><strong>Account:</strong> {accountCopy(session)}</p>
          </div>

          {#if session.last_error_class || session.last_error_text}
            <div class="rounded-2xl border border-danger-line bg-danger-surface p-3 text-sm text-danger">
              <strong>{session.last_error_class ?? 'Last error'}</strong>
              {#if session.last_error_text}<p class="m-0 break-words">{session.last_error_text}</p>{/if}
            </div>
          {/if}

          <div class="grid gap-3 xl:grid-cols-3">
            <div class="grid content-start gap-3 rounded-2xl border border-line bg-soft/40 p-3">
              <strong>QR login</strong>
              <p class="m-0 text-sm text-muted">Start QR login, scan the returned Telegram URL, then complete the attempt id from the success notice.</p>
              <form method="POST" action="?/startQrLogin" class="grid gap-2">
                <input type="hidden" name="session_id" value={session.id} />
                <Button type="submit" variant="secondary">Start QR login</Button>
              </form>
              <form method="POST" action="?/completeQrLogin" class="grid gap-2 border-t border-line pt-2">
                <input type="hidden" name="session_id" value={session.id} />
                <FormRow label="Attempt id"><Input name="attempt_id" placeholder="paste QR attempt id" required /></FormRow>
                <Input name="note" placeholder="login note" />
                <Button type="submit">Complete QR login</Button>
              </form>
            </div>

            <div class="grid content-start gap-3 rounded-2xl border border-line bg-soft/40 p-3">
              <strong>Phone + code login</strong>
              <p class="m-0 text-sm text-muted">Send a code to the account phone. The full number is submitted once and never rendered back.</p>
              <form method="POST" action="?/startPhoneLogin" class="grid gap-2">
                <input type="hidden" name="session_id" value={session.id} />
                <FormRow label="Phone number"><Input name="phone_number" autocomplete="tel" placeholder="+15551234567" required /></FormRow>
                <Input name="note" placeholder="login note" />
                <Button type="submit" variant="secondary">Send code</Button>
              </form>
              <form method="POST" action="?/completePhoneCodeLogin" class="grid gap-2 border-t border-line pt-2">
                <input type="hidden" name="session_id" value={session.id} />
                <FormRow label="Attempt id"><Input name="attempt_id" placeholder="paste phone attempt id" required /></FormRow>
                <FormRow label="Telegram code"><Input name="code" autocomplete="one-time-code" required /></FormRow>
                <Input name="note" placeholder="login note" />
                <Button type="submit">Complete code login</Button>
              </form>
            </div>

            <form method="POST" action="?/completePhonePasswordLogin" class="grid content-start gap-3 rounded-2xl border border-line bg-soft/40 p-3">
              <input type="hidden" name="session_id" value={session.id} />
              <strong>2FA password</strong>
              <p class="m-0 text-sm text-muted">Use this only after code login reports that Telegram requires the account password.</p>
              <FormRow label="Attempt id"><Input name="attempt_id" placeholder="same phone attempt id" required /></FormRow>
              <FormRow label="2FA password"><Input name="password" type="password" autocomplete="current-password" required /></FormRow>
              <Input name="note" placeholder="login note" />
              <Button type="submit">Finish password login</Button>
            </form>
          </div>

          <div class="grid gap-3 xl:grid-cols-[minmax(0,1.4fr)_minmax(260px,0.7fr)_minmax(260px,0.7fr)]">
            <form method="POST" action="?/updateSession" class="grid gap-3 rounded-2xl border border-line bg-soft/40 p-3">
              <input type="hidden" name="session_id" value={session.id} />
              <strong>Patch policy/status</strong>
              <div class="grid gap-2 md:grid-cols-3">
                <FormRow label="Display name"><Input name="display_name" value={session.display_name} required /></FormRow>
                <FormRow label="Status">
                  <Select name="status">
                    {#each sessionStatuses as status}
                      <option value={status} selected={status === session.status}>{status}</option>
                    {/each}
                  </Select>
                </FormRow>
                <FormRow label="Max requests/sec"><Input name="max_requests_per_second" type="number" min="0.1" step="0.1" value={session.max_requests_per_second} required /></FormRow>
              </div>
              <div class="grid gap-2 md:grid-cols-3">
                <FormRow label="Flood wait until"><Input name="flood_wait_until" value={session.flood_wait_until ?? ''} placeholder="ISO datetime or blank" /></FormRow>
                <FormRow label="Last error class"><Input name="last_error_class" value={session.last_error_class ?? ''} /></FormRow>
                <FormRow label="Audit note"><Input name="note" placeholder="optional" /></FormRow>
              </div>
              <FormRow label="Last error text"><Textarea name="last_error_text" rows={2} value={session.last_error_text ?? ''} /></FormRow>
              <div class="grid gap-2 md:grid-cols-3">
                <label class="inline-flex items-center gap-2 text-chiptext"><input name="enabled" type="checkbox" checked={session.enabled} /> Enabled</label>
                <label class="inline-flex items-center gap-2 text-chiptext"><input name="catchup_enabled" type="checkbox" checked={session.catchup_enabled} /> Catch-up</label>
                <label class="inline-flex items-center gap-2 text-chiptext"><input name="live_enabled" type="checkbox" checked={session.live_enabled} /> Live</label>
                <label class="inline-flex items-center gap-2 text-chiptext"><input name="engagement_enabled" type="checkbox" checked={session.engagement_enabled} /> Engagement</label>
                <label class="inline-flex items-center gap-2 text-chiptext"><input name="clear_error" type="checkbox" /> Clear error</label>
              </div>
              <Button type="submit">Save session</Button>
            </form>

            <form method="POST" action="?/validateSession" class="grid content-start gap-3 rounded-2xl border border-line bg-soft/40 p-3">
              <input type="hidden" name="session_id" value={session.id} />
              <strong>Validate access</strong>
              <FormRow label="Optional channel">
                <Select name="source_channel_id">
                  <option value="">Session only</option>
                  {#each channels as channel (channel.id)}
                    <option value={channel.id}>{channelLabel(channel)}</option>
                  {/each}
                </Select>
              </FormRow>
              <Input name="note" placeholder="validation note" />
              <Button type="submit" variant="secondary">Validate</Button>
            </form>

            <form method="POST" action="?/deleteSession" class="grid content-start gap-3 rounded-2xl border border-danger-line bg-danger-surface p-3">
              <input type="hidden" name="session_id" value={session.id} />
              <strong>Delete session</strong>
              <p class="m-0 text-sm text-danger">Assigned channels become orphaned and non-indexable. Paste the session id to confirm.</p>
              <Input name="confirmation" placeholder={session.id} autocomplete="off" required />
              <Textarea name="note" rows={2} placeholder="required operator context is recommended" />
              <Button type="submit" variant="danger">Delete and orphan channels</Button>
            </form>
          </div>
        </article>
      {/each}
    </div>
  {/if}
</AdminPanel>

<AdminPanel title="Channels by Session">
  {#if data.telegramAdmin.groups.length === 0}
    <EmptyState title="No channel groups" message="Telegram channel groups could not load." />
  {:else}
    <div class="grid gap-4">
      {#each data.telegramAdmin.groups as group, groupIndex (group.telegram_session?.id ?? `orphaned-${groupIndex}`)}
        <section class={group.is_orphaned ? 'grid gap-3 rounded-3xl border border-danger-line bg-danger-surface/60 p-4' : 'grid gap-3 rounded-3xl border border-line bg-paper p-4'}>
          <div class="flex flex-wrap items-start justify-between gap-2">
            <div>
              <h3 class="m-0 text-xl font-black tracking-[-0.03em]">{groupTitle(group)}</h3>
              <p class="m-0 text-sm text-muted">
                {#if group.is_orphaned}
                  Orphaned channels have no session, are shown as non-indexable, and cannot enable crawler controls until assigned.
                {:else if group.telegram_session}
                  Assigned to {group.telegram_session.name} · session status {group.telegram_session.status}
                {/if}
              </p>
            </div>
            {#if group.is_orphaned}
              <Badge class="border-danger-line bg-danger-surface text-danger">non-indexable group</Badge>
            {:else if group.telegram_session}
              <Badge tone={group.telegram_session.enabled ? 'success' : 'neutral'}>{group.telegram_session.enabled ? 'session enabled' : 'session disabled'}</Badge>
            {/if}
          </div>

          {#if group.channels.length === 0}
            <p class="m-0 text-sm text-muted">No channels in this group.</p>
          {:else}
            <div class="grid gap-3">
              {#each group.channels as channel (channel.id)}
                <article class="grid gap-3 rounded-2xl border border-line bg-paper p-3">
                  <div class="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <strong>{channel.title}</strong>
                      <p class="m-0 text-sm text-muted">{channel.platform}:{channel.platform_id} · {channel.username ? `@${channel.username}` : 'no username'} · ID {channel.id}</p>
                      <p class="m-0 text-sm text-muted">{channel.operational_status} · {freshnessCopy(channel)} · last fetched {formatTimestamp(channel.last_fetched_at)}</p>
                    </div>
                    <div class="flex flex-wrap gap-2">
                      <Badge class={channel.is_indexable ? '' : 'border-danger-line bg-danger-surface text-danger'}>{channel.is_indexable ? 'indexable' : 'non-indexable'}</Badge>
                      <Badge>{channel.is_orphaned ? 'orphaned' : 'assigned'}</Badge>
                      <Badge>{channel.is_active ? 'active row' : 'dead row'}</Badge>
                    </div>
                  </div>

                  <div class="grid gap-3 xl:grid-cols-[minmax(0,1.2fr)_minmax(260px,0.6fr)_minmax(260px,0.6fr)]">
                    <form method="POST" action="?/updateChannel" class="grid gap-2 rounded-2xl border border-line bg-soft/40 p-3">
                      <input type="hidden" name="channel_id" value={channel.id} />
                      <strong>Edit indexing controls</strong>
                      {#if channel.is_orphaned}
                        <p class="m-0 text-sm text-danger">Orphaned channels cannot be indexable. Assign this channel before enabling catch-up, live, or engagement.</p>
                      {/if}
                      <div class="grid gap-2 md:grid-cols-4">
                        <label class="inline-flex items-center gap-2 text-chiptext"><input name="catchup_enabled" type="checkbox" checked={channel.is_orphaned ? false : channel.catchup_enabled} disabled={channel.is_orphaned} /> Catch-up</label>
                        <label class="inline-flex items-center gap-2 text-chiptext"><input name="live_enabled" type="checkbox" checked={channel.is_orphaned ? false : channel.live_enabled} disabled={channel.is_orphaned} /> Live</label>
                        <label class="inline-flex items-center gap-2 text-chiptext"><input name="engagement_enabled" type="checkbox" checked={channel.is_orphaned ? false : channel.engagement_enabled} disabled={channel.is_orphaned} /> Engagement</label>
                        <FormRow label="Catch-up limit"><Input name="catchup_message_limit" type="number" min="1" max="10000" value={channel.catchup_message_limit} required /></FormRow>
                      </div>
                      <Button type="submit">Save channel controls</Button>
                    </form>

                    <div class="grid gap-2 rounded-2xl border border-line bg-soft/40 p-3">
                      <form method="POST" action="?/assignChannel" class="grid gap-2">
                        <input type="hidden" name="channel_id" value={channel.id} />
                        <strong>{channel.is_orphaned ? 'Assign channel' : 'Move channel'}</strong>
                        <Select name="telegram_session_id" required disabled={data.telegramAdmin.sessions.length === 0}>
                          <option value="">Choose session</option>
                          {#each data.telegramAdmin.sessions as session (session.id)}
                            <option value={session.id} selected={session.id === channel.telegram_session_id}>{sessionLabel(session)}</option>
                          {/each}
                        </Select>
                        <Input name="note" placeholder="move/assignment note" />
                        <Button type="submit" variant="secondary" disabled={data.telegramAdmin.sessions.length === 0}>Assign or move</Button>
                      </form>

                      <form method="POST" action="?/orphanChannel" class="grid gap-2 border-t border-line pt-2">
                        <input type="hidden" name="channel_id" value={channel.id} />
                        <Input name="note" placeholder="orphan note" disabled={channel.is_orphaned} />
                        <Button type="submit" variant="secondary" disabled={channel.is_orphaned}>Orphan and disable indexing</Button>
                      </form>
                    </div>

                    <div class="grid content-start gap-2 rounded-2xl border border-line bg-soft/40 p-3">
                      {#if channel.is_active}
                        <form method="POST" action="?/toggleChannel" class="flex flex-wrap gap-2">
                          <input type="hidden" name="channel_id" value={channel.id} />
                          <input type="hidden" name="paused" value={channel.is_paused ? 'false' : 'true'} />
                          <Button type="submit" variant="secondary">{channel.is_paused ? 'Resume' : 'Pause'}</Button>
                        </form>
                        <form method="POST" action="?/markChannelDead" class="grid gap-2 border-t border-line pt-2">
                          <strong>Mark dead</strong>
                          <p class="m-0 text-sm text-muted">Paste the channel id to confirm. This preserves crawler checkpoint state.</p>
                          <input type="hidden" name="channel_id" value={channel.id} />
                          <Input name="confirmation" placeholder={channel.id} autocomplete="off" required />
                          <Button type="submit" variant="danger">Mark dead</Button>
                        </form>
                      {:else}
                        <Badge>Removed from crawl</Badge>
                      {/if}
                    </div>
                  </div>
                </article>
              {/each}
            </div>
          {/if}
        </section>
      {/each}
    </div>
  {/if}
</AdminPanel>
