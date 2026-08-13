<script lang="ts">
  import '../app.css';
  import { page } from '$app/state';
  import PageViewTracker from '$lib/analytics/PageViewTracker.svelte';
  import { provideMemeInteractionQueue } from '$lib/analytics/interaction-queue';
  import { provideAuthState } from '$lib/auth-state';
  import AppShell from '$lib/features/app-shell/AppShell.svelte';
  import TelegramLoginModal from '$lib/features/auth/TelegramLoginModal.svelte';
  import { provideMemeVideoCoordinator } from '$lib/features/memes/meme-video-coordinator';
  import { provideMemeExposureScope } from '$lib/features/memes/meme-exposure-scope';
  import { provideMemeActionState } from '$lib/meme-action-state';
  import TelegramMiniAppBootstrap from '$lib/TelegramMiniAppBootstrap.svelte';
  import TooltipProvider from '$lib/ui/tooltip/Provider.svelte';
  import { provideViewerCapabilities, viewerCapabilitiesFromSession } from '$lib/viewer-capabilities';
  import { onMount, type Snippet } from 'svelte';
  import type { LayoutData } from './$types';

  let { data, children }: { data: LayoutData; children: Snippet } = $props();
  let loginOpen = $state(false);

  const authState = provideAuthState(() => ({ session: data.session ?? null, sessionError: data.sessionError }));
  const session = $derived($authState.session);
  const sessionError = $derived($authState.sessionError);
  const memeActionState = provideMemeActionState(() => session?.user.id ?? null);
  const memeExposureScope = provideMemeExposureScope(page.url.pathname);
  const memeInteractionQueue = provideMemeInteractionQueue();
  provideMemeVideoCoordinator();

  $effect(() => {
    authState.syncFromServer({ session: data.session ?? null, sessionError: data.sessionError });
  });

  $effect(() => {
    const viewerId = session?.user.id ?? null;
    memeActionState.syncViewer(viewerId);
    memeInteractionQueue.syncViewer(viewerId);
  });

  $effect(() => {
    memeExposureScope.syncPage(page.url.pathname);
  });

  provideViewerCapabilities(() => viewerCapabilitiesFromSession(session));

  onMount(() => {
    const nonce = globalThis.crypto?.randomUUID?.() ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    memeExposureScope.beginClientVisit(nonce);
    return memeInteractionQueue.startBrowserLifecycle();
  });
</script>

<svelte:head>
  <meta name="theme-color" content="#f7f7f8" media="(prefers-color-scheme: light)" />
  <meta name="theme-color" content="#16181d" media="(prefers-color-scheme: dark)" />
</svelte:head>

<TelegramMiniAppBootstrap />
<PageViewTracker />

<TooltipProvider delayDuration={500}>
  <AppShell {session} {sessionError} searchMemeCount={data.searchMemeCount} currentPath={page.url.pathname} onLoginClick={() => (loginOpen = true)}>
    {@render children()}
  </AppShell>
  <TelegramLoginModal bind:open={loginOpen} />
</TooltipProvider>
