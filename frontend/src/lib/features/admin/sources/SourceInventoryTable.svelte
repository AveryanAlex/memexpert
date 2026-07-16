<script lang="ts">
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import type { AdminSourceChannelRead, AdminTelegramSessionRead } from '$lib/api/types';
  import { ActionLink, Badge, Button } from '$lib/ui';
  import SourceSortHeader from './SourceSortHeader.svelte';
  import {
    DEFAULT_SOURCE_INVENTORY_SORT,
    nextSourceInventorySort,
    sortSourceInventory,
    toSourceCardViewModel,
    type SourceInventorySortKey
  } from './view-model';

  let {
    sources,
    telegramAccounts
  }: {
    sources: AdminSourceChannelRead[];
    telegramAccounts: AdminTelegramSessionRead[];
  } = $props();

  let sort = $state({ ...DEFAULT_SOURCE_INVENTORY_SORT });
  const sortedSources = $derived(sortSourceInventory(sources, telegramAccounts, sort));
  const sortAnnouncement = $derived(
    sort.key === 'health'
      ? `Sources sorted by health, ${sort.direction === 'ascending' ? 'attention first' : 'healthy first'}.`
      : `Sources sorted by ${sortLabel(sort.key)}, ${sort.direction}.`
  );

  function setSort(key: SourceInventorySortKey): void {
    sort = nextSourceInventorySort(sort, key);
  }

  function sortLabel(key: SourceInventorySortKey): string {
    switch (key) {
      case 'source': return 'source';
      case 'health': return 'health and account';
      case 'latest_post': return 'latest post';
      case 'last_fetched': return 'last fetched';
      case 'memes': return 'memes';
      case 'posts': return 'posts';
      case 'subscribers': return 'subscribers';
    }
  }

  function latestPostLabel(source: AdminSourceChannelRead): string {
    return source.latest_post_at ? formatAdminTimestamp(source.latest_post_at) : 'No posts observed';
  }

  function metric(value: number | null): string {
    return value === null ? 'Unknown' : value.toLocaleString('en-US');
  }
</script>

<p class="sr-only" aria-live="polite">{sortAnnouncement}</p>

<!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard access to the horizontal table overflow) -->
<div
  class="overflow-x-auto rounded-3xl border border-line bg-paper focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
  role="region"
  aria-label="Source inventory table"
  tabindex="0"
>
  <table class="w-full min-w-[82rem] border-collapse text-left text-sm">
    <caption class="sr-only">Configured sources with health, activity, catalog counts, and operator actions.</caption>
    <thead class="bg-soft text-chiptext">
      <tr>
        <SourceSortHeader label="Source" sortKey="source" activeKey={sort.key} direction={sort.direction} onSort={setSort} class="min-w-56 bg-soft lg:sticky lg:left-0 lg:z-20" />
        <SourceSortHeader label="Health / account" sortKey="health" activeKey={sort.key} direction={sort.direction} onSort={setSort} class="min-w-60" />
        <SourceSortHeader label="Latest post" sortKey="latest_post" activeKey={sort.key} direction={sort.direction} onSort={setSort} class="min-w-48" />
        <SourceSortHeader label="Last fetched" sortKey="last_fetched" activeKey={sort.key} direction={sort.direction} onSort={setSort} class="min-w-36" />
        <SourceSortHeader label="Memes" sortKey="memes" activeKey={sort.key} direction={sort.direction} onSort={setSort} />
        <SourceSortHeader label="Posts" sortKey="posts" activeKey={sort.key} direction={sort.direction} onSort={setSort} />
        <SourceSortHeader label="Subscribers" sortKey="subscribers" activeKey={sort.key} direction={sort.direction} onSort={setSort} />
        <th scope="col" class="min-w-52 bg-soft px-3 py-3 font-black lg:sticky lg:right-0 lg:z-20"><span class="sr-only">Actions</span></th>
      </tr>
    </thead>
    <tbody>
      {#each sortedSources as source (source.id)}
        {@const model = toSourceCardViewModel(source, telegramAccounts)}
        <tr class="group border-t border-line align-top">
          <th scope="row" class="bg-paper px-3 py-4 lg:sticky lg:left-0 lg:z-10 lg:group-hover:bg-soft/40">
            <a class="font-black underline decoration-2 underline-offset-4" href={`/admin/sources/${source.id}`}>{source.title}</a>
            <p class="mb-0 mt-1 text-xs text-muted">{model.handleLabel} · {model.platformLabel}</p>
          </th>
          <td class="px-3 py-4">
            <Badge
              tone={model.status === 'Healthy' ? 'success' : 'neutral'}
              class={model.status === 'Needs attention' || source.backfill_status === 'failed' ? 'border-danger-line bg-danger-surface text-danger' : ''}
            >{model.status}</Badge>
            <p class="mb-0 mt-2 max-w-64 text-xs text-muted">{model.statusDetail}</p>
            <p class="mb-0 mt-2 text-xs"><strong>Account:</strong> {model.assignedAccountLabel}</p>
            {#if source.backfill_status !== 'idle'}
              <p class="mb-0 mt-1 text-xs {source.backfill_status === 'failed' ? 'text-danger' : 'text-muted'}">
                Backfill: {source.backfill_status.replaceAll('_', ' ')}
                {#if source.backfill_requested_count > 0} · {source.backfill_scanned_count.toLocaleString('en-US')} / {source.backfill_requested_count.toLocaleString('en-US')}{/if}
              </p>
            {/if}
          </td>
          <td class="px-3 py-4">{latestPostLabel(source)}</td>
          <td class="px-3 py-4">{model.lastFetchLabel.replace('Last fetched ', '')}</td>
          <td class="px-3 py-4 text-right tabular-nums">{metric(source.meme_count)}</td>
          <td class="px-3 py-4 text-right tabular-nums">{metric(source.observed_post_count)}</td>
          <td class="px-3 py-4 text-right tabular-nums">{metric(source.subscriber_count)}</td>
          <td class="bg-paper px-3 py-4 lg:sticky lg:right-0 lg:z-10 lg:group-hover:bg-soft/40">
            <div class="flex flex-wrap justify-end gap-2">
              <ActionLink href={`/admin/sources/${source.id}`} variant="secondary" size="compact" aria-label={`Manage ${source.title}`}>Manage</ActionLink>
              {#if model.canToggle && model.toggleLabel}
                <form method="POST" action="?/toggleSourceChannel">
                  <input type="hidden" name="channel_id" value={source.id} />
                  <input type="hidden" name="paused" value={source.is_paused ? 'false' : 'true'} />
                  <Button type="submit" variant="secondary" size="compact" aria-label={`${model.toggleLabel} ${source.title}`}>{model.toggleLabel}</Button>
                </form>
              {/if}
            </div>
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>
