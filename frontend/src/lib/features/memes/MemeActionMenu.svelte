<script lang="ts">
  import { browser } from '$app/environment';
  import {
    fetchCollections,
    favoriteMeme,
    pinMeme,
    recordMemeDownload,
    recordMemeShare,
    reportMeme,
    removeSavedMeme,
    saveMeme,
    saveMemeToCollection,
    unfavoriteMeme,
    unpinMeme,
    type RemoveActionResponse
  } from '$lib/api/client';
  import type { ModerationReason, PublicMemeCardRead, PublicMemeDetailRead, WebCollectionSummaryRead } from '$lib/api/types';
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
  import { cn } from '$lib/ui/styles';
  import { readViewerCapabilities } from '$lib/viewer-capabilities';
  import * as Menu from '$lib/ui/dropdown-menu';
  import { Bookmark, Copy, Download, Flag, Folder, Heart, MoreHorizontal, Pin, Send } from '@lucide/svelte';

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

  let favorited = $state(false);
  let saved = $state(false);
  let pinned = $state(false);
  let likeCount = $state(0);
  let pending = $state<MemeActionKind | null>(null);
  let errorMessage = $state<string | null>(null);
  let reportOpen = $state(false);
  let reportReason = $state<ModerationReason>('spam');
  let reportNote = $state('');
  let hydrated = $state(false);
  let saveMenuOpen = $state(false);
  let collectionsRequestForOpen = $state(false);
  let collectionsLoading = $state(false);
  let collectionsReady = $state(false);
  let collectionsError = $state<string | null>(null);
  let writableCollections = $state<WebCollectionSummaryRead[]>([]);

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

  syncStateFromMeme();

  $effect(() => {
    syncStateFromMeme();
  });

  $effect(() => {
    hydrated = true;
  });

  $effect(() => {
    if (saveMenuOpen && !collectionsRequestForOpen) {
      collectionsRequestForOpen = true;
      void loadCollections();
    } else if (!saveMenuOpen && collectionsRequestForOpen) {
      collectionsRequestForOpen = false;
    }
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
      onFavoriteChange?.(next);
    });
  }

  async function toggleSave() {
    const next = !saved;
    await runAction(next ? 'save' : 'unsave', async () => {
      await (next ? saveMeme(actionRequest) : removeSavedMeme(actionRequest));
      saved = next;
    });
  }

  async function loadCollections() {
    if (!browser || collectionsLoading) return;

    collectionsLoading = true;
    collectionsReady = false;
    collectionsError = null;
    try {
      const response = await fetchCollections({ fetch, baseUrl: window.location.origin });
      writableCollections = response.collections.filter((collection) => collection.capabilities.can_add_memes);
      collectionsReady = true;
    } catch (error) {
      collectionsError = error instanceof Error ? error.message : 'Could not load your collections.';
    } finally {
      collectionsLoading = false;
    }
  }

  function selectCollection(collection: WebCollectionSummaryRead) {
    void saveToCollection(collection);
  }

  async function saveToCollection(collection: WebCollectionSummaryRead) {
    if (!browser) return;

    await runAction('save', async () => {
      await saveMemeToCollection({
        fetch,
        baseUrl: window.location.origin,
        collectionId: collection.collection.id,
        memeId: meme.id,
        body: actionBody
      });
      saved = true;
    });
  }

  async function togglePin() {
    const next = !pinned;
    await runAction(next ? 'pin' : 'unpin', async () => {
      await (next ? pinMeme(actionRequest) : unpinMeme(actionRequest));
      pinned = next;
    });
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

  function wasRemoved(response: unknown): boolean {
    return Boolean((response as RemoveActionResponse | null)?.removed ?? true);
  }
</script>

{#snippet saveCollectionMenu()}
  <Menu.Content align={isCardSurface ? 'center' : 'start'} class="max-h-80 w-[min(18rem,calc(100vw-2rem))] overflow-y-auto">
    <p class="m-0 px-3 pb-2 pt-1 text-xs font-extrabold uppercase tracking-wide text-muted">Save to collection</p>
    {#if collectionsLoading || (!collectionsReady && !collectionsError)}
      <Menu.Item disabled>Loading collections…</Menu.Item>
    {:else if collectionsError}
      <p class="m-0 rounded-xl px-3 py-2.5 text-sm font-semibold text-danger" role="alert">{collectionsError} Close and reopen to retry.</p>
    {:else if writableCollections.length === 0}
      <Menu.Item disabled>No writable collections available</Menu.Item>
    {:else}
      {#each writableCollections as collection (collection.collection.id)}
        <Menu.Item onSelect={() => selectCollection(collection)} disabled={pending !== null}>
          <Folder class="size-4 shrink-0" aria-hidden="true" />
          <span class="min-w-0 truncate">{collection.collection.title}</span>
        </Menu.Item>
      {/each}
    {/if}
  </Menu.Content>
{/snippet}

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
        disabled={interactionsDisabled}
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
      <Menu.Root bind:open={saveMenuOpen}>
        <Menu.Trigger
          variant="ghost"
          aria-label="Save to collection"
          title="Save to collection"
          aria-pressed={saved}
          disabled={interactionsDisabled}
        >
          <Bookmark class={cn('size-5 shrink-0', saved && 'fill-current text-accent')} aria-hidden="true" />
        </Menu.Trigger>
        {@render saveCollectionMenu()}
      </Menu.Root>
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
          <Menu.Item onSelect={toggleFavorite} disabled={pending !== null}>
            <Heart class="size-4" aria-hidden="true" />
            {favorited ? 'Remove favorite' : 'Favorite'} meme
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
          <Menu.Item onSelect={shareTelegram} disabled={pending !== null}><Send class="size-4" aria-hidden="true" />Send to Telegram</Menu.Item>
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
    </div>
  {:else}
    {#if isDetailSurface}
      <div class="flex flex-wrap gap-2" aria-label="Primary meme actions">
        <Button variant="secondary" type="button" aria-pressed={favorited} disabled={interactionsDisabled} onclick={toggleFavorite}>
          <Heart class={cn('size-4', favorited && 'fill-current text-danger')} aria-hidden="true" />
          Favorite ({likeCount})
        </Button>
        <Menu.Root bind:open={saveMenuOpen}>
          <Menu.Trigger variant="secondary" aria-label="Save to collection" aria-pressed={saved} disabled={interactionsDisabled}>
            <Bookmark class={cn('size-4', saved && 'fill-current text-accent')} aria-hidden="true" />
            {saved ? 'Saved' : 'Save'}
          </Menu.Trigger>
          {@render saveCollectionMenu()}
        </Menu.Root>
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
      <Menu.Item onSelect={toggleFavorite} disabled={pending !== null}>
        <Heart class="size-4" aria-hidden="true" />
        {favorited ? 'Remove favorite' : 'Favorite'} meme
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
      <Menu.Item onSelect={shareTelegram} disabled={pending !== null}><Send class="size-4" aria-hidden="true" />Send to Telegram</Menu.Item>
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
