<script lang="ts">
  import '../app.css';
  import { page } from '$app/state';
  import AppShell from '$lib/features/app-shell/AppShell.svelte';
  import TelegramLoginModal from '$lib/features/auth/TelegramLoginModal.svelte';
  import TelegramMiniAppBootstrap from '$lib/TelegramMiniAppBootstrap.svelte';
  import TooltipProvider from '$lib/ui/tooltip/Provider.svelte';
  import { provideViewerCapabilities, viewerCapabilitiesFromSession } from '$lib/viewer-capabilities';
  import type { Snippet } from 'svelte';
  import type { LayoutData } from './$types';

  let { data, children }: { data: LayoutData; children: Snippet } = $props();
  let loginOpen = $state(false);

  provideViewerCapabilities(() => viewerCapabilitiesFromSession(data.session ?? null));
</script>

<TelegramMiniAppBootstrap />

<TooltipProvider delayDuration={500}>
  <AppShell session={data.session} sessionError={data.sessionError} currentPath={page.url.pathname} onLoginClick={() => (loginOpen = true)}>
    {@render children()}
  </AppShell>
  <TelegramLoginModal bind:open={loginOpen} session={data.session} />
</TooltipProvider>
