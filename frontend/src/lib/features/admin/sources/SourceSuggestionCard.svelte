<script lang="ts">
  import { Badge, Button } from '$lib/ui';
  import type { ChannelSuggestionRead } from '$lib/api/types';
  import { relativeTimestamp } from './view-model';

  let { suggestion, onAddTelegram }: { suggestion: ChannelSuggestionRead; onAddTelegram?: (suggestion: ChannelSuggestionRead) => void } = $props();

  const isTelegram = $derived(suggestion.platform === 'telegram');
  const platformLabel = $derived(suggestion.platform === 'vk' ? 'VK' : suggestion.platform[0].toUpperCase() + suggestion.platform.slice(1));
</script>

<article class="grid gap-3 rounded-3xl border border-line bg-paper p-4">
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div class="min-w-0">
      <h3 class="m-0 text-lg font-black tracking-[-0.03em]">{platformLabel} suggestion</h3>
      <p class="m-0 break-all text-sm text-muted">{suggestion.channel_url}</p>
    </div>
    <Badge>{relativeTimestamp(suggestion.created_at)}</Badge>
  </div>

  {#if suggestion.admin_note}
    <p class="m-0 rounded-2xl border border-line bg-soft/40 p-3 text-sm text-muted"><strong class="text-ink">Note:</strong> {suggestion.admin_note}</p>
  {/if}

  {#if isTelegram}
    <p class="m-0 text-sm text-muted">Add this source to prefill the public channel form. The source and suggestion are saved together.</p>
    <div class="flex flex-wrap gap-2">
      <Button type="button" variant="secondary" onclick={() => onAddTelegram?.(suggestion)}>Add this source</Button>
      <form method="POST" action="?/reviewSuggestion">
        <input type="hidden" name="suggestion_id" value={suggestion.id} />
        <input type="hidden" name="decision" value="reject" />
        <Button type="submit" variant="secondary">Reject</Button>
      </form>
    </div>
  {:else}
    <p class="m-0 rounded-2xl border border-line bg-soft/40 p-3 text-sm text-muted">
      {platformLabel} crawler support is unavailable. This suggestion cannot be added as a source yet.
    </p>
    <form method="POST" action="?/reviewSuggestion">
      <input type="hidden" name="suggestion_id" value={suggestion.id} />
      <input type="hidden" name="decision" value="reject" />
      <Button type="submit" variant="secondary">Reject</Button>
    </form>
  {/if}
</article>
