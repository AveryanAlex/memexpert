<script lang="ts">
  import '../app.css';
  import { page } from '$app/state';
  import { provideAuthState } from '$lib/auth-state';
  import AppShell from '$lib/features/app-shell/AppShell.svelte';
  import TelegramLoginModal from '$lib/features/auth/TelegramLoginModal.svelte';
  import TelegramMiniAppBootstrap from '$lib/TelegramMiniAppBootstrap.svelte';
  import TooltipProvider from '$lib/ui/tooltip/Provider.svelte';
  import { provideViewerCapabilities, viewerCapabilitiesFromSession } from '$lib/viewer-capabilities';
  import type { Snippet } from 'svelte';
  import type { LayoutData } from './$types';

  let { data, children }: { data: LayoutData; children: Snippet } = $props();
  let loginOpen = $state(false);

  const authState = provideAuthState(() => ({ session: data.session ?? null, sessionError: data.sessionError }));
  const session = $derived($authState.session);
  const sessionError = $derived($authState.sessionError);

  $effect(() => {
    authState.syncFromServer({ session: data.session ?? null, sessionError: data.sessionError });
  });

  provideViewerCapabilities(() => viewerCapabilitiesFromSession(session));
</script>

<svelte:head>
  <meta name="theme-color" content="#f7f7f8" media="(prefers-color-scheme: light)" />
  <meta name="theme-color" content="#16181d" media="(prefers-color-scheme: dark)" />
</svelte:head>

<TelegramMiniAppBootstrap />

<TooltipProvider delayDuration={500}>
  <AppShell {session} {sessionError} currentPath={page.url.pathname} onLoginClick={() => (loginOpen = true)}>
    {@render children()}
  </AppShell>
  <TelegramLoginModal bind:open={loginOpen} />
</TooltipProvider>
