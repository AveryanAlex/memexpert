<script lang="ts">
  import { browser } from '$app/environment';
  import {
    favoriteMeme,
    pinMeme,
    recordMemeDownload,
    recordMemeShare,
    reportMeme,
    removeSavedMeme,
    saveMeme,
    unfavoriteMeme,
    unpinMeme,
    type RemoveActionResponse
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
  import { Button, Select, Textarea } from '$lib/ui';
  import { readViewerCapabilities } from '$lib/viewer-capabilities';
  import * as Menu from '$lib/ui/dropdown-menu';
  import { Bookmark, Copy, Download, Flag, Heart, MoreHorizontal, Pin, Send } from '@lucide/svelte';

  interface Props {
    meme: PublicMemeCardRead | PublicMemeDetailRead;
    href?: string;
    attribution?: MemeActionAttribution | null;
    showPrimary?: boolean;
    showSharing?: boolean;
    compact?: boolean;
  }

  let { meme, href = memeHref(meme), attribution = null, showPrimary = false, showSharing = false, compact = false }: Props = $props();

  const viewerCapabilities = readViewerCapabilities();

  let favorited = $state(false);
  let saved = $state(false);
  let pinned = $state(false);
  let likeCount = $state(0);
  let pending = $state<MemeActionKind | null>(null);
  let statusMessage = $state<string | null>(null);
  let reportOpen = $state(false);
  let reportReason = $state<ModerationReason>('spam');
  let reportNote = $state('');

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

  syncStateFromMeme();

  $effect(() => {
    syncStateFromMeme();
  });

  function syncStateFromMeme() {
    favorited = meme.viewer_has_favorited;
    saved = meme.viewer_has_saved;
    pinned = meme.viewer_has_pinned;
    likeCount = meme.like_count;
  }

  async function toggleFavorite() {
    const next = !favorited;
    await runAction(next ? 'favorite' : 'unfavorite', async () => {
      const wasFavorited = favorited;
      const response = next ? await favoriteMeme(actionRequest) : await unfavoriteMeme(actionRequest);
      favorited = next;
      if (next && !wasFavorited) {
        likeCount += 1;
      } else if (!next && wasFavorited && wasRemoved(response)) {
        likeCount = Math.max(0, likeCount - 1);
      }
      statusMessage = next ? 'Liked.' : 'Unliked.';
    });
  }

  async function toggleSave() {
    const next = !saved;
    await runAction(next ? 'save' : 'unsave', async () => {
      await (next ? saveMeme(actionRequest) : removeSavedMeme(actionRequest));
      saved = next;
      statusMessage = next ? 'Saved to your active collection.' : 'Removed from your active collection.';
    });
  }

  async function togglePin() {
    const next = !pinned;
    await runAction(next ? 'pin' : 'unpin', async () => {
      await (next ? pinMeme(actionRequest) : unpinMeme(actionRequest));
      pinned = next;
      statusMessage = next ? 'Pinned.' : 'Unpinned.';
    });
  }

  async function copyLink() {
    await runAction('copy', async () => {
      if (!browser) return;
      await copyText(canonicalUrl);
      statusMessage = 'Link copied.';
    });
  }

  async function shareTelegram() {
    await runAction('telegram', async () => {
      if (!browser) return;
      void recordMemeShare(actionRequest).catch(() => undefined);
      window.open(telegramShareUrl(canonicalUrl, title), '_blank', 'noopener,noreferrer');
      statusMessage = 'Opened Telegram share.';
    });
  }

  async function downloadMeme() {
    if (!browser || !downloadUrl) {
      statusMessage = actionFailureMessage('download', null);
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
      statusMessage = 'Download started.';
    });
  }

  function openReportForm() {
    reportOpen = true;
    statusMessage = null;
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
      statusMessage = 'Report submitted. Thanks for helping keep MemeXpert clean.';
    });
  }

  function handleReportSubmit(event: SubmitEvent) {
    event.preventDefault();
    void submitReport();
  }

  async function runAction(action: MemeActionKind, callback: () => Promise<void>) {
    if (pending) return;
    pending = action;
    statusMessage = null;
    try {
      await callback();
    } catch (error) {
      statusMessage = actionFailureMessage(action, error);
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

  function wasRemoved(response: unknown): boolean {
    return Boolean((response as RemoveActionResponse | null)?.removed ?? true);
  }
</script>

<div class={compact ? 'flex flex-wrap items-start gap-2' : 'my-3 flex flex-wrap items-start gap-2'}>
  {#if showPrimary}
    <div class="flex flex-wrap gap-2" aria-label="Primary meme actions">
      <Button variant="secondary" type="button" disabled={pending !== null} onclick={toggleFavorite}>
        <Heart class="size-4" aria-hidden="true" />
        {favorited ? 'Unlike' : 'Like'} ({likeCount})
      </Button>
      <Button variant="secondary" type="button" disabled={pending !== null} onclick={toggleSave}>
        <Bookmark class="size-4" aria-hidden="true" />
        {saved ? 'Saved' : 'Save'}
      </Button>
      {#if canPin}
        <Button variant="secondary" type="button" disabled={pending !== null} onclick={togglePin}>
          <Pin class="size-4" aria-hidden="true" />
          {pinned ? 'Unpin' : 'Pin'}
        </Button>
      {:else}
        <p class="m-0 inline-flex items-center gap-2 rounded-full border border-line bg-soft px-4 py-2 text-sm font-extrabold text-muted">
          <Pin class="size-4" aria-hidden="true" />
          Pin requires a full account
        </p>
      {/if}
    </div>
  {/if}

  {#if showSharing}
    <div class="flex flex-wrap gap-2" aria-label="Share and safety actions">
      <Button variant="secondary" type="button" disabled={pending !== null} onclick={shareTelegram}>
        <Send class="size-4" aria-hidden="true" />
        Share to Telegram
      </Button>
      <Button variant="secondary" type="button" disabled={pending !== null} onclick={copyLink}>
        <Copy class="size-4" aria-hidden="true" />
        Copy link
      </Button>
      <Button variant="secondary" type="button" disabled={pending !== null || !canDownload} onclick={downloadMeme}>
        <Download class="size-4" aria-hidden="true" />
        {canDownload ? 'Download' : 'Download unavailable'}
      </Button>
      <Button variant="ghost" type="button" disabled={pending !== null} onclick={openReportForm}>
        <Flag class="size-4" aria-hidden="true" />
        Report
      </Button>
    </div>
  {/if}

  <Menu.Root>
    <Menu.Trigger aria-label={compact ? `Actions for ${title}` : 'Meme actions'} disabled={pending !== null}>
      <MoreHorizontal class="size-5" aria-hidden="true" />
    </Menu.Trigger>
    <Menu.Content>
      <Menu.Item onSelect={toggleFavorite} disabled={pending !== null}>
        <Heart class="size-4" aria-hidden="true" />
        {favorited ? 'Unlike' : 'Like'} meme
      </Menu.Item>
      <Menu.Item onSelect={toggleSave} disabled={pending !== null}>
        <Bookmark class="size-4" aria-hidden="true" />
        {saved ? 'Remove save' : 'Save'}
      </Menu.Item>
      {#if canPin}
        <Menu.Item onSelect={togglePin} disabled={pending !== null}>
          <Pin class="size-4" aria-hidden="true" />
          {pinned ? 'Unpin' : 'Pin'}
        </Menu.Item>
      {/if}
      <Menu.Separator />
      <Menu.Item onSelect={shareTelegram}><Send class="size-4" aria-hidden="true" />Share to Telegram</Menu.Item>
      <Menu.Item onSelect={copyLink}><Copy class="size-4" aria-hidden="true" />Copy link</Menu.Item>
      <Menu.Item onSelect={downloadMeme} disabled={!canDownload}>
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

  {#if statusMessage}
    <p class="basis-full text-sm text-muted" role="status">{statusMessage}</p>
  {/if}
</div>
