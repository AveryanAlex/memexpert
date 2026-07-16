<script lang="ts">
  import { isReadyTelegramAccount } from '$lib/features/admin/telegram/readiness';
  import type { AdminSourceChannelRead, AdminTelegramSessionRead } from '$lib/api/types';
  import { Button, FormRow, Input, Select } from '$lib/ui';
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import { toSourceCardViewModel } from './view-model';

  let {
    source,
    telegramAccounts
  }: {
    source: AdminSourceChannelRead;
    telegramAccounts: AdminTelegramSessionRead[];
  } = $props();

  const model = $derived(toSourceCardViewModel(source, telegramAccounts));
  const supportsTelegramCrawler = $derived(source.platform === 'telegram');
  const currentAccount = $derived(telegramAccounts.find((account) => account.id === source.telegram_session_id) ?? null);
  const readyAccounts = $derived(telegramAccounts.filter((account) => isReadyTelegramAccount(account)));
  const hasUnavailableCurrentAssignment = $derived(Boolean(currentAccount && !isReadyTelegramAccount(currentAccount)));
  const assignmentAccounts = $derived(hasUnavailableCurrentAssignment && currentAccount ? [currentAccount, ...readyAccounts] : readyAccounts);
  const canConfigureIngestion = $derived(Boolean(
    supportsTelegramCrawler && source.is_active && !source.is_paused && currentAccount && isReadyTelegramAccount(currentAccount)
  ));
</script>

<div class="grid gap-3">
  <AdvancedSection title="Diagnostics" description="Technical identifiers and crawler checkpoints for troubleshooting.">
    <dl class="m-0 grid gap-2 text-sm md:grid-cols-2">
      <div><dt class="font-extrabold">Source ID</dt><dd class="m-0 break-all text-muted">{source.id}</dd></div>
      <div><dt class="font-extrabold">Platform ID</dt><dd class="m-0 break-all text-muted">{source.platform_id}</dd></div>
      <div><dt class="font-extrabold">Fetch state</dt><dd class="m-0 text-muted">{source.freshness_status}</dd></div>
      <div><dt class="font-extrabold">Checkpoint</dt><dd class="m-0 text-muted">{source.last_read_post_id ?? 'none'}</dd></div>
    </dl>
  </AdvancedSection>

  <AdvancedSection title="Ingestion settings" description="Control catch-up, live collection, and engagement tracking.">
    {#if supportsTelegramCrawler}
      <form method="POST" action="?/updateSourceChannelIngestion" class="grid gap-3">
        <input type="hidden" name="channel_id" value={source.id} />
        {#if !canConfigureIngestion}
          <p class="m-0 text-sm text-danger">Assign a ready account before enabling ingestion for this source.</p>
        {/if}
        <div class="grid gap-2 md:grid-cols-3">
          <label class="inline-flex items-center gap-2 text-chiptext"><input name="catchup_enabled" type="checkbox" checked={canConfigureIngestion ? source.catchup_enabled : false} disabled={!canConfigureIngestion} /> Catch up</label>
          <label class="inline-flex items-center gap-2 text-chiptext"><input name="live_enabled" type="checkbox" checked={canConfigureIngestion ? source.live_enabled : false} disabled={!canConfigureIngestion} /> Watch new posts</label>
          <label class="inline-flex items-center gap-2 text-chiptext"><input name="engagement_enabled" type="checkbox" checked={canConfigureIngestion ? source.engagement_enabled : false} disabled={!canConfigureIngestion} /> Track engagement</label>
        </div>
        <FormRow label="Catch-up limit"><Input name="catchup_message_limit" type="number" min="1" max="10000" value={source.catchup_message_limit} required disabled={!canConfigureIngestion} /></FormRow>
        <Button type="submit" variant="secondary" disabled={!canConfigureIngestion}>Save ingestion settings</Button>
      </form>
    {:else}
      <p class="m-0 text-sm text-muted">{model.platformLabel} crawler support is unavailable, so ingestion settings cannot be changed.</p>
    {/if}
  </AdvancedSection>

  <AdvancedSection title="Assignment" description="Choose the Telegram account that fetches this source.">
    {#if supportsTelegramCrawler}
      <div class="grid gap-3 lg:grid-cols-2">
        <form method="POST" action="?/assignSourceChannel" class="grid gap-3">
          <input type="hidden" name="channel_id" value={source.id} />
          <FormRow label={source.is_orphaned ? 'Assign account' : 'Move to account'}>
            <Select name="telegram_session_id" required disabled={readyAccounts.length === 0}>
              <option value="">Choose a Telegram account</option>
              {#each assignmentAccounts as account (account.id)}
                <option value={account.id} selected={account.id === source.telegram_session_id} disabled={!isReadyTelegramAccount(account)}>
                  {account.display_name}{isReadyTelegramAccount(account) ? '' : ' (unavailable)'}
                </option>
              {/each}
            </Select>
          </FormRow>
          {#if hasUnavailableCurrentAssignment}
            <p class="m-0 text-sm text-danger">Current account: {model.assignedAccountLabel}. Choose a ready account before saving.</p>
          {/if}
          <FormRow label="Assignment note (optional)"><Input name="note" placeholder="Why this account is being used" /></FormRow>
          <Button type="submit" variant="secondary" disabled={readyAccounts.length === 0}>{source.is_orphaned ? 'Assign account' : 'Move source'}</Button>
        </form>
        <form method="POST" action="?/orphanSourceChannel" class="grid content-start gap-3 border-t border-line pt-3 lg:border-l lg:border-t-0 lg:pl-3 lg:pt-0">
          <input type="hidden" name="channel_id" value={source.id} />
          <p class="m-0 text-sm text-muted">Remove its account and disable ingestion until it is assigned again.</p>
          <FormRow label="Reason (optional)"><Input name="note" placeholder="Why this source is unassigned" disabled={source.is_orphaned} /></FormRow>
          <Button type="submit" variant="secondary" disabled={source.is_orphaned}>Remove account</Button>
        </form>
      </div>
      <AdvancedSection title="Validate source access" description="Check whether a ready Telegram account can access this specific source.">
        <form method="POST" action="?/validateSourceAccount" class="grid gap-3">
          <input type="hidden" name="source_channel_id" value={source.id} />
          <FormRow label="Telegram account">
            <Select name="telegram_session_id" required disabled={readyAccounts.length === 0}>
              <option value="">Choose a ready account</option>
              {#each readyAccounts as account (account.id)}
                <option value={account.id} selected={account.id === source.telegram_session_id}>{account.display_name}</option>
              {/each}
            </Select>
          </FormRow>
          <FormRow label="Validation note (optional)"><Input name="note" placeholder="Why this access check is needed" /></FormRow>
          <Button type="submit" variant="secondary" disabled={readyAccounts.length === 0}>Validate source access</Button>
        </form>
      </AdvancedSection>
    {:else}
      <p class="m-0 text-sm text-muted">{model.platformLabel} crawler support is unavailable, so no Telegram account can be assigned.</p>
    {/if}
  </AdvancedSection>

  <AdvancedSection title="Remove source" description="Stop crawling this source while preserving its checkpoint history." danger>
    {#if source.is_active}
      <form method="POST" action="?/markSourceChannelDead" class="grid gap-3">
        <input type="hidden" name="channel_id" value={source.id} />
        <FormRow label="Confirm source removal" hint="Paste the source ID from Diagnostics to confirm.">
          <Input name="confirmation" placeholder="Source ID from Diagnostics" autocomplete="off" required />
        </FormRow>
        <Button type="submit" variant="danger">Remove from crawling</Button>
      </form>
    {:else}
      <p class="m-0 text-sm text-danger">This source has already been removed from crawling.</p>
    {/if}
  </AdvancedSection>
</div>
