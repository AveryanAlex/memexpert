<script lang="ts">
  import { browser } from '$app/environment';
  import { DropdownMenu } from 'bits-ui';
  import {
    favoriteMeme,
    pinMeme,
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
    memeDownloadUrl,
    memeHref,
    memeTitle,
    telegramShareUrl,
    type MemeActionKind
  } from '$lib/memeActions';

  interface Props {
    meme: PublicMemeCardRead | PublicMemeDetailRead;
    href?: string;
    showPrimary?: boolean;
    compact?: boolean;
  }

  let { meme, href = memeHref(meme), showPrimary = false, compact = false }: Props = $props();

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
  const actionRequest = $derived({ fetch, memeId: meme.id });

  $effect(() => {
    favorited = meme.viewer_has_favorited;
    saved = meme.viewer_has_saved;
    pinned = meme.viewer_has_pinned;
    likeCount = meme.like_count;
  });

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

  function shareTelegram() {
    if (!browser) return;
    window.open(telegramShareUrl(canonicalUrl, title), '_blank', 'noopener,noreferrer');
    statusMessage = 'Opened Telegram share.';
  }

  function downloadMeme() {
    if (!browser || !downloadUrl) {
      statusMessage = actionFailureMessage('download', null);
      return;
    }

    const link = document.createElement('a');
    link.href = downloadUrl;
    link.download = `${title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'meme'}`;
    link.rel = 'noopener noreferrer';
    document.body.append(link);
    link.click();
    link.remove();
    statusMessage = 'Download started.';
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

<div class={compact ? 'meme-actions compact' : 'meme-actions'}>
  {#if showPrimary}
    <div class="primary-actions" aria-label="Primary meme actions">
      <button class="button-link secondary action-button" type="button" disabled={pending !== null} onclick={toggleFavorite}>
        {favorited ? 'Unlike' : 'Like'} ({likeCount})
      </button>
      <button class="button-link secondary action-button" type="button" disabled={pending !== null} onclick={toggleSave}>
        {saved ? 'Saved' : 'Save'}
      </button>
      <button class="button-link secondary action-button" type="button" disabled={pending !== null} onclick={togglePin}>
        {pinned ? 'Unpin' : 'Pin'}
      </button>
    </div>
  {/if}

  <DropdownMenu.Root>
    <DropdownMenu.Trigger class="menu-trigger" aria-label="Meme actions" disabled={pending !== null}>
      <span aria-hidden="true">...</span>
    </DropdownMenu.Trigger>
    <DropdownMenu.Portal>
      <DropdownMenu.Content class="action-menu" align="end" sideOffset={8}>
        <DropdownMenu.Item class="action-menu-item" onSelect={toggleFavorite} disabled={pending !== null}>
          {favorited ? 'Unlike' : 'Like'} meme
        </DropdownMenu.Item>
        <DropdownMenu.Item class="action-menu-item" onSelect={toggleSave} disabled={pending !== null}>
          {saved ? 'Remove save' : 'Save'}
        </DropdownMenu.Item>
        <DropdownMenu.Item class="action-menu-item" onSelect={togglePin} disabled={pending !== null}>
          {pinned ? 'Unpin' : 'Pin'}
        </DropdownMenu.Item>
        <DropdownMenu.Separator class="action-menu-separator" />
        <DropdownMenu.Item class="action-menu-item" onSelect={shareTelegram}>Share to Telegram</DropdownMenu.Item>
        <DropdownMenu.Item class="action-menu-item" onSelect={copyLink}>Copy link</DropdownMenu.Item>
        <DropdownMenu.Item class="action-menu-item" onSelect={downloadMeme} disabled={!canDownload}>
          {canDownload ? 'Download' : 'Download unavailable'}
        </DropdownMenu.Item>
        <DropdownMenu.Separator class="action-menu-separator" />
        <DropdownMenu.Item
          class="action-menu-item"
          style="color: var(--danger-color, #b42318);"
          onSelect={openReportForm}
          disabled={pending !== null}
        >
          Report meme
        </DropdownMenu.Item>
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  </DropdownMenu.Root>

  {#if reportOpen}
    <form class="report-panel" onsubmit={handleReportSubmit}>
      <label class="report-label" for={`report-reason-${meme.id}`}>Reason</label>
      <select id={`report-reason-${meme.id}`} class="report-select" bind:value={reportReason} disabled={pending !== null}>
        {#each reportReasons as reason}
          <option value={reason.value}>{reason.label}</option>
        {/each}
      </select>

      <label class="report-label" for={`report-note-${meme.id}`}>Optional note</label>
      <textarea
        id={`report-note-${meme.id}`}
        class="report-note"
        bind:value={reportNote}
        maxlength="2048"
        rows="3"
        placeholder="Add context for moderators"
        disabled={pending !== null}
      ></textarea>

      <div class="report-actions">
        <button class="button-link secondary action-button" type="button" onclick={closeReportForm} disabled={pending !== null}>
          Cancel
        </button>
        <button class="button-link action-button" type="submit" disabled={pending !== null}>
          {pending === 'report' ? 'Submitting...' : 'Submit report'}
        </button>
      </div>
    </form>
  {/if}

  {#if statusMessage}
    <p class="action-status" role="status">{statusMessage}</p>
  {/if}
</div>

<style>
  .report-panel {
    margin-top: 0.75rem;
    display: grid;
    gap: 0.45rem;
    max-width: 22rem;
    padding: 0.75rem;
    border: 1px solid color-mix(in srgb, currentColor 16%, transparent);
    border-radius: 0.8rem;
    background: var(--surface-elevated, Canvas);
    box-shadow: 0 12px 28px color-mix(in srgb, black 12%, transparent);
  }

  .report-label {
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.02em;
    text-transform: uppercase;
  }

  .report-select,
  .report-note {
    width: 100%;
    box-sizing: border-box;
    border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
    border-radius: 0.6rem;
    padding: 0.55rem 0.65rem;
    font: inherit;
    color: inherit;
    background: var(--surface, Canvas);
  }

  .report-note {
    resize: vertical;
  }

  .report-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
  }
</style>
