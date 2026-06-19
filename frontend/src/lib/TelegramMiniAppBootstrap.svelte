<script lang="ts">
  import { goto, invalidateAll } from '$app/navigation';
  import { onMount } from 'svelte';
  import {
    extractTelegramMiniAppBootstrapState,
    readTelegramWebApp,
    telegramLaunchParamsFromUrl,
    type TelegramMiniAppBootstrapState,
    type TelegramWebAppLike
  } from '$lib/telegram-miniapp';

  const ROUTED_START_PARAM_STORAGE_PREFIX = 'memexpert:telegram-miniapp:routed-start-param:';

  onMount(() => {
    const webApp = readTelegramWebApp(window);
    if (!webApp) {
      return;
    }

    const state = extractTelegramMiniAppBootstrapState(webApp, telegramLaunchParamsFromUrl(window.location));
    if (!state) {
      return;
    }

    applyTelegramShellState(state);
    callWebAppMethod(webApp.ready);
    callWebAppMethod(webApp.expand);
    const expandedState = extractTelegramMiniAppBootstrapState(readTelegramWebApp(window), telegramLaunchParamsFromUrl(window.location));
    const shellState = expandedState ?? state;
    applyTelegramShellState(shellState);
    void authenticateAndRoute(shellState);
  });

  function applyTelegramShellState(state: TelegramMiniAppBootstrapState) {
    applyElementState(document.documentElement, state);
    applyElementState(document.body, state);
  }

  function applyElementState(element: HTMLElement, state: TelegramMiniAppBootstrapState) {
    for (const className of state.classNames) {
      element.classList.add(className);
    }
    for (const [attribute, value] of Object.entries(state.dataAttributes)) {
      element.setAttribute(attribute, value);
    }
    for (const [property, value] of Object.entries(state.cssVariables)) {
      element.style.setProperty(property, value);
    }
  }

  function callWebAppMethod(method: TelegramWebAppLike['ready'] | TelegramWebAppLike['expand']) {
    try {
      method?.();
    } catch {
      // Telegram host methods are best-effort and should never block the web shell.
    }
  }

  async function authenticateAndRoute(state: TelegramMiniAppBootstrapState) {
    if (state.initData) {
      await authenticateWithTelegram(state.initData);
    }

    await routeStartParamOnce(state);
  }

  async function authenticateWithTelegram(initData: string) {
    try {
      const response = await fetch('/telegram-miniapp/auth', {
        method: 'POST',
        headers: {
          accept: 'application/json',
          'content-type': 'application/json',
          'x-requested-with': 'XMLHttpRequest'
        },
        credentials: 'include',
        body: JSON.stringify({ initData })
      });

      if (response.ok) {
        await invalidateAll();
      }
    } catch {
      // Keep browsing with the current web session when Telegram auth is unavailable.
    }
  }

  async function routeStartParamOnce(state: TelegramMiniAppBootstrapState) {
    if (!state.startParam || !state.startRoute || hasRoutedStartParam(state.startParam)) {
      return;
    }

    markStartParamRouted(state.startParam);
    const currentPath = `${window.location.pathname}${window.location.search}`;
    if (currentPath !== state.startRoute) {
      await goto(state.startRoute, { replaceState: true });
    }
  }

  function hasRoutedStartParam(startParam: string): boolean {
    try {
      return sessionStorage.getItem(startParamStorageKey(startParam)) === '1';
    } catch {
      return false;
    }
  }

  function markStartParamRouted(startParam: string) {
    try {
      sessionStorage.setItem(startParamStorageKey(startParam), '1');
    } catch {
      // Storage can be disabled in embedded browsers; one navigation is still safe.
    }
  }

  function startParamStorageKey(startParam: string): string {
    return `${ROUTED_START_PARAM_STORAGE_PREFIX}${encodeURIComponent(startParam)}`;
  }
</script>
