<script lang="ts">
  import { browser } from '$app/environment';
  import {
    favoriteMeme,
    pinMeme,
    recordMemeDownload,
    recordMemeShare,
    reportMeme,
    unfavoriteMeme,
    unpinMeme
  } from '$lib/api/client';
  import type { ModerationReason, PublicMemeCardRead, PublicMemeDetailRead } from '$lib/api/types';
  import {
    actionFailureMessage,
    canonicalMemeUrl,
    memeActionAttributionBody,
    memeDownloadUrl,
    memeHref,
    memeTitle,
    telegramShareUrl,
    type MemeActionAttribution,
    type MemeActionKind
  } from '$lib/memeActions';
  import { readMemeActionState } from '$lib/meme-action-state';
  import { Button, Select, Textarea } from '$lib/ui';
  import { cn } from '$lib/ui/styles';
  import { readViewerCapabilities } from '$lib/viewer-capabilities';
  import * as Menu from '$lib/ui/dropdown-menu';
  import { Copy, Download, Flag, Heart, MoreHorizontal, Pin, Send } from '@lucide/svelte';
  import SaveCollectionChooser from './SaveCollectionChooser.svelte';

  export type MemeActionSurface = 'card' | 'detail' | 'overflow';

  interface Props {
    meme: PublicMemeCardRead | PublicMemeDetailRead;
    href?: string;
    attribution?: MemeActionAttribution | null;
    surface?: MemeActionSurface;
    /** @deprecated Use the explicit action surface instead. */
    showPrimary?: boolean;
    /** @deprecated Use the explicit action surface instead. */
    showSharing?: boolean;
    /** @deprecated Use the card action surface instead. */
    compact?: boolean;
    onFavoriteChange?: (favorited: boolean) => void;
  }

  let {
    meme,
    href = memeHref(meme),
    attribution = null,
    surface = undefined,
    showPrimary = false,
    showSharing = false,
    compact = false,
    onFavoriteChange
  }: Props = $props();

  const viewerCapabilities = readViewerCapabilities();
  const memeActionState = readMemeActionState();

  let pending = $state<MemeActionKind | null>(null);
  let errorMessage = $state<string | null>(null);
  let reportOpen = $state(false);
  let reportReason = $state<ModerationReason>('spam');
  let reportNote = $state('');
  let hydrated = $state(false);

  const reportReasons: Array<{ value: ModerationReason; label: string }> = [
    { value: 'spam', label: 'Spam or scam' },
    { value: 'nsfw', label: 'Nudity or explicit content' },
    { value: 'harassment', label: 'Harassment or hate' },
    { value: 'illegal', label: 'Illegal content' },
    { value: 'copyright', label: 'Copyright issue' },
    { value: 'other', label: 'Other' }
  ];

  const title = $derived(memeTitle(meme));
  const canonicalUrl = $derived(browser ? canonicalMemeUrl(meme, window.location.origin) : href);
  const downloadUrl = $derived(memeDownloadUrl(meme));
  const canDownload = $derived(Boolean(downloadUrl));
  const canPin = $derived(viewerCapabilities().canPinMemes);
  const actionBody = $derived(memeActionAttributionBody(attribution));
  const actionRequest = $derived({ fetch, memeId: meme.id, body: actionBody });
  const actionSurface = $derived(surface ?? (showPrimary || showSharing ? 'detail' : compact ? 'card' : 'overflow'));
  const isCardSurface = $derived(actionSurface === 'card');
  const isDetailSurface = $derived(actionSurface === 'detail');
  const menuLabel = $derived(isCardSurface ? `Actions for ${title}` : 'Meme actions');
  const interactionsDisabled = $derived(!hydrated || pending !== null);
  const sharedState = $derived($memeActionState[meme.id]);
  const favorited = $derived(sharedState?.favorited ?? meme.viewer_has_favorited);
  const favoritePending = $derived(sharedState?.favoritePending ?? false);
  const saved = $derived(sharedState?.saved ?? meme.viewer_has_saved);
  const savedCollectionIds = $derived(sharedState?.savedCollectionIds);
  const pinned = $derived(sharedState?.pinned ?? meme.viewer_has_pinned);
  const pinPending = $derived(sharedState?.pinPending ?? false);
  const likeCount = $derived(sharedState?.likeCount ?? meme.like_count);

  $effect(() => {
    hydrated = true;
  });

  async function toggleFavorite() {
    if (pending) return;
    const operation = memeActionState.beginOperation(meme.id, 'favorite');
    if (!operation) return;

    const next = !favorited;
    let completed = false;
    await runAction(next ? 'favorite' : 'unfavorite', async () => {
      const response = next ? await favoriteMeme(actionRequest) : await unfavoriteMeme(actionRequest);
      completed = memeActionState.completeOperation(operation, {
        favorited: response.favorited,
        likeCount: response.like_count
      });
      if (completed) onFavoriteChange?.(response.favorited);
    });
    if (!completed) memeActionState.completeOperation(operation);
  }

  function updateSavedCollections(collectionIds: readonly string[]) {
    memeActionState.publish(meme.id, {
      saved: collectionIds.length > 0,
      savedCollectionIds: [...collectionIds]
    });
  }

  async function togglePin() {
    if (pending) return;
    const operation = memeActionState.beginOperation(meme.id, 'pin');
    if (!operation) return;

    const next = !pinned;
    let completed = false;
    await runAction(next ? 'pin' : 'unpin', async () => {
      await (next ? pinMeme(actionRequest) : unpinMeme(actionRequest));
      completed = memeActionState.completeOperation(operation, { pinned: next });
    });
    if (!completed) memeActionState.completeOperation(operation);
  }

  async function copyLink() {
    await runAction('copy', async () => {
      if (!browser) return;
      await copyText(canonicalUrl);
    });
  }

  async function shareTelegram() {
    await runAction('telegram', async () => {
      if (!browser) return;
      void recordMemeShare(actionRequest).catch(() => undefined);
      window.open(telegramShareUrl(canonicalUrl, title), '_blank', 'noopener,noreferrer');
    });
  }

  async function downloadMeme() {
    if (!browser || !downloadUrl) {
      errorMessage = actionFailureMessage('download', null);
      return;
    }

    await runAction('download', async () => {
      void recordMemeDownload(actionRequest).catch(() => undefined);
      const link = document.createElement('a');
      link.href = downloadUrl;
      link.download = `${title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'meme'}`;
      link.rel = 'noopener noreferrer';
      document.body.append(link);
      link.click();
      link.remove();
    });
  }

  function openReportForm() {
    reportOpen = true;
    errorMessage = null;
  }

  function closeReportForm() {
    reportOpen = false;
    reportNote = '';
    reportReason = 'spam';
  }

  async function submitReport() {
    await runAction('report', async () => {
      await reportMeme({
        ...actionRequest,
        reason: reportReason,
        note: reportNote
      });
      reportOpen = false;
      reportNote = '';
    });
  }

  function handleReportSubmit(event: SubmitEvent) {
    event.preventDefault();
    void submitReport();
  }

  async function runAction(action: MemeActionKind, callback: () => Promise<void>) {
    if (pending) return;
    pending = action;
    errorMessage = null;
    try {
      await callback();
    } catch (error) {
      errorMessage = actionFailureMessage(action, error);
    } finally {
      pending = null;
    }
  }

  async function copyText(text: string): Promise<void> {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }

    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    document.body.append(textarea);
    textarea.select();
    const copied = document.execCommand('copy');
    textarea.remove();
    if (!copied) {
      throw new Error('Clipboard permission was denied.');
    }
  }

</script>

<div class={isCardSurface ? 'border-t border-line px-3 py-2.5' : compact ? 'flex flex-wrap items-start gap-2' : 'my-3 flex flex-wrap items-start gap-2'}>
  {#if isCardSurface}
    <div class="grid grid-cols-5 items-center gap-1" aria-label="Meme actions">
      <Button
        class="h-10 w-full min-w-0 px-0 py-0"
        variant="ghost"
        size="micro"
        type="button"
        aria-label={favorited ? 'Remove favorite' : 'Favorite'}
        title={favorited ? 'Remove favorite' : 'Favorite'}
        aria-pressed={favorited}
        disabled={interactionsDisabled || favoritePending}
        onclick={toggleFavorite}
      >
        <Heart class={cn('size-5 shrink-0', favorited && 'fill-current text-danger')} aria-hidden="true" />
      </Button>
      <Button
        class="h-10 w-full min-w-0 px-0 py-0"
        variant="ghost"
        size="micro"
        type="button"
        aria-label={canDownload ? 'Download' : 'Download unavailable'}
        title={canDownload ? 'Download' : 'Download unavailable'}
        disabled={!canDownload || interactionsDisabled}
        onclick={downloadMeme}
      >
        <Download class="size-5 shrink-0" aria-hidden="true" />
      </Button>
      <SaveCollectionChooser
        memeId={meme.id}
        {title}
        {attribution}
        surface="card"
        {saved}
        {savedCollectionIds}
        disabled={interactionsDisabled}
        onMembershipChange={updateSavedCollections}
      />
      <Button
        class="h-10 w-full min-w-0 px-0 py-0"
        variant="ghost"
        size="micro"
        type="button"
        aria-label="Send"
        title="Send"
        disabled={interactionsDisabled}
        onclick={shareTelegram}
      >
        <Send class="size-5 shrink-0" aria-hidden="true" />
      </Button>
      <Menu.Root>
        <Menu.Trigger variant="ghost" aria-label={menuLabel} title="More actions" disabled={interactionsDisabled} class="text-muted hover:text-ink">
          <MoreHorizontal class="size-5" aria-hidden="true" />
        </Menu.Trigger>
        <Menu.Content>
          {#if canPin}
            <Menu.Item onSelect={togglePin} disabled={pending !== null || pinPending}>
              <Pin class="size-4" aria-hidden="true" />
              {pinned ? 'Unpin' : 'Pin'}
            </Menu.Item>
          {/if}
          <Menu.Item onSelect={copyLink} disabled={pending !== null}><Copy class="size-4" aria-hidden="true" />Copy link</Menu.Item>
          <Menu.Separator />
          <Menu.Item tone="danger" onSelect={openReportForm} disabled={pending !== null}>
            <Flag class="size-4" aria-hidden="true" />
            Report meme
          </Menu.Item>
        </Menu.Content>
      </Menu.Root>
    </div>
  {:else}
    {#if isDetailSurface}
      <div class="flex flex-wrap gap-2" aria-label="Primary meme actions">
        <Button variant="secondary" type="button" aria-pressed={favorited} disabled={interactionsDisabled || favoritePending} onclick={toggleFavorite}>
          <Heart class={cn('size-4', favorited && 'fill-current text-danger')} aria-hidden="true" />
          Favorite ({likeCount})
        </Button>
        <SaveCollectionChooser
          memeId={meme.id}
          {title}
          {attribution}
          surface="detail"
          {saved}
          {savedCollectionIds}
          disabled={interactionsDisabled}
          onMembershipChange={updateSavedCollections}
        />
        <Button variant="secondary" type="button" disabled={interactionsDisabled} onclick={shareTelegram}>
          <Send class="size-4" aria-hidden="true" />
          Send
        </Button>
      </div>
    {/if}

    <Menu.Root>
    <Menu.Trigger aria-label={compact ? `Actions for ${title}` : 'Meme actions'} disabled={interactionsDisabled}>
      <MoreHorizontal class="size-5" aria-hidden="true" />
    </Menu.Trigger>
    <Menu.Content>
      {#if canPin}
        <Menu.Item onSelect={togglePin} disabled={pending !== null || pinPending}>
          <Pin class="size-4" aria-hidden="true" />
          {pinned ? 'Unpin' : 'Pin'}
        </Menu.Item>
      {/if}
      <Menu.Item onSelect={copyLink} disabled={pending !== null}><Copy class="size-4" aria-hidden="true" />Copy link</Menu.Item>
      <Menu.Item onSelect={downloadMeme} disabled={!canDownload || pending !== null}>
        <Download class="size-4" aria-hidden="true" />
        {canDownload ? 'Download' : 'Download unavailable'}
      </Menu.Item>
      <Menu.Separator />
      <Menu.Item tone="danger" onSelect={openReportForm} disabled={pending !== null}>
        <Flag class="size-4" aria-hidden="true" />
        Report meme
      </Menu.Item>
    </Menu.Content>
    </Menu.Root>
  {/if}

  {#if reportOpen}
    <form class="grid w-full max-w-sm gap-2 rounded-2xl border border-line bg-paper p-3 shadow-warm" onsubmit={handleReportSubmit}>
      <label class="grid gap-1 text-xs font-extrabold uppercase tracking-wide" for={`report-reason-${meme.id}`}>Reason</label>
      <Select id={`report-reason-${meme.id}`} bind:value={reportReason} disabled={pending !== null}>
        {#each reportReasons as reason}
          <option value={reason.value}>{reason.label}</option>
        {/each}
      </Select>

      <label class="grid gap-1 text-xs font-extrabold uppercase tracking-wide" for={`report-note-${meme.id}`}>Optional note</label>
      <Textarea
        id={`report-note-${meme.id}`}
        bind:value={reportNote}
        maxlength={2048}
        rows={3}
        placeholder="Add context for moderators"
        disabled={pending !== null}
      />

      <div class="flex justify-end gap-2">
        <Button variant="secondary" size="compact" type="button" onclick={closeReportForm} disabled={pending !== null}>Cancel</Button>
        <Button size="compact" type="submit" disabled={pending !== null}>{pending === 'report' ? 'Submitting...' : 'Submit report'}</Button>
      </div>
    </form>
  {/if}

  {#if errorMessage}
    <p class="basis-full text-sm text-danger" role="alert">{errorMessage}</p>
  {/if}
</div>
