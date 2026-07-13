<script lang="ts">
  import { invalidateAll } from '$app/navigation';
  import { ApiError, updateUserPreferences } from '$lib/api/client';
  import type { UserLanguage } from '$lib/api/types';
  import { readAuthState } from '$lib/auth-state';
  import { profileCapabilities, profileStats } from '$lib/profile/view-model';
  import { ActionLink, Badge, Button, Card, Notice, Select } from '$lib/ui';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();

  const authState = readAuthState(() => ({ session: data.session ?? null, sessionError: data.sessionError }));
  const session = $derived($authState.session);
  const sessionError = $derived($authState.sessionError);

  let nsfwPending = $state(false);
  let nsfwMessage = $state<string | null>(null);
  let selectedLanguage = $state<UserLanguage>('any');
  let languagePending = $state(false);
  let languageMessage = $state<string | null>(null);

  const LANGUAGE_OPTIONS: Array<{ value: UserLanguage; label: string }> = [
    { value: 'any', label: 'Any language' },
    { value: 'en', label: 'English' },
    { value: 'ru', label: 'Russian' }
  ];

  const capabilities = $derived(profileCapabilities(session));
  const stats = $derived(profileStats(data.profileStats));
  const accountLabel = $derived(
    session
      ? session.user.account_type === 'full'
        ? 'Connected account'
        : 'Guest account'
      : 'Account unavailable'
  );
  const telegramConnected = $derived(Boolean(session?.linked_providers.telegram_linked));

  $effect(() => {
    selectedLanguage = session?.user.language ?? 'any';
  });

  async function disableNsfw() {
    nsfwPending = true;
    nsfwMessage = null;

    try {
      const user = await updateUserPreferences({ fetch, body: { nsfw_enabled: false } });
      authState.updateUser(user);
      nsfwMessage = 'Sensitive content is hidden again.';
      await invalidateAll();
    } catch (error) {
      nsfwMessage = error instanceof ApiError || error instanceof Error ? error.message : 'Could not update sensitive-content preference.';
    } finally {
      nsfwPending = false;
    }
  }

  async function changeLanguage(event: Event) {
    const nextLanguage = (event.currentTarget as HTMLSelectElement).value;
    if (!isUserLanguage(nextLanguage)) {
      selectedLanguage = session?.user.language ?? 'any';
      return;
    }

    if (!session || nextLanguage === session.user.language) {
      selectedLanguage = nextLanguage;
      return;
    }

    const previousLanguage = session.user.language;
    languagePending = true;
    languageMessage = null;
    selectedLanguage = nextLanguage;

    try {
      const user = await updateUserPreferences({ fetch, body: { language: nextLanguage } });
      authState.updateUser(user);
      languageMessage = `Language preference updated to ${languageOptionLabel(nextLanguage)}.`;
      await invalidateAll();
    } catch (error) {
      selectedLanguage = previousLanguage;
      languageMessage = error instanceof ApiError || error instanceof Error ? error.message : 'Could not update language preference.';
    } finally {
      languagePending = false;
    }
  }

  function isUserLanguage(value: string): value is UserLanguage {
    return LANGUAGE_OPTIONS.some((option) => option.value === value);
  }

  function languageOptionLabel(value: UserLanguage): string {
    return LANGUAGE_OPTIONS.find((option) => option.value === value)?.label ?? value;
  }
</script>

<section class="mb-5 grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(280px,0.45fr)]" aria-labelledby="profile-title">
  <div>
    <Badge>Account</Badge>
    <h1 id="profile-title" class="m-0 mt-3 text-[clamp(2rem,5vw,4rem)] font-black leading-[0.95] tracking-[-0.055em]">Account</h1>
    <p class="m-0 mt-2 max-w-2xl text-muted">Manage your connection and browsing preferences. <a class="font-extrabold underline decoration-2 underline-offset-4" href="/library">Open Saved</a>.</p>
  </div>

  <Card class="grid gap-3 shadow-none" aria-labelledby="telegram-title">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p class="m-0 text-sm font-extrabold text-muted">{accountLabel}</p>
        <h2 id="telegram-title" class="m-0 text-xl font-black tracking-[-0.03em]">Telegram</h2>
      </div>
      {#if telegramConnected}
        <Badge tone="success">Telegram connected</Badge>
      {:else}
        <Badge>Not connected</Badge>
      {/if}
    </div>
    {#if sessionError}
      <p class="m-0 text-sm text-muted" role="status">{sessionError}</p>
    {:else if telegramConnected}
      <p class="m-0 text-sm text-muted">Telegram is connected for cross-device access.</p>
    {:else if capabilities.showConnectTelegram}
      <p class="m-0 text-sm text-muted">Connect Telegram to keep this account across devices.</p>
      <ActionLink size="compact" href="/account/telegram?returnTo=/profile">Connect Telegram</ActionLink>
    {:else}
      <p class="m-0 text-sm text-muted">Telegram is not connected to this account.</p>
    {/if}
  </Card>
</section>

<section class="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(280px,0.65fr)]" aria-label="Account preferences and activity">
  <Card class="grid gap-4 shadow-none" aria-labelledby="profile-settings-title">
    <div>
      <h2 id="profile-settings-title" class="m-0 text-xl font-black tracking-[-0.03em]">Preferences</h2>
      <p class="m-0 text-sm text-muted">Choose how account-aware browsing behaves.</p>
    </div>

    <div class="rounded-xl border border-line bg-soft/50 p-4">
      <p class="m-0 font-black">Language preference</p>
      <p class="m-0 mb-3 text-sm text-muted">Choose the account language used by account-aware surfaces when supported.</p>
      <label class="grid max-w-[420px] gap-2 font-extrabold text-chiptext">
        <span>Profile language</span>
        <Select bind:value={selectedLanguage} onchange={changeLanguage} disabled={!session || languagePending}>
          {#each LANGUAGE_OPTIONS as option}
            <option value={option.value}>{option.label}</option>
          {/each}
        </Select>
      </label>
      {#if languageMessage}
        <p class="m-0 mt-3 text-sm text-muted" role="status">{languageMessage}</p>
      {/if}
    </div>

    <div class="rounded-xl border border-line bg-soft/50 p-4">
      <p class="m-0 font-black">Sensitive content</p>
      {#if session?.user.nsfw_enabled}
        <p class="m-0 mt-1 text-sm text-muted">Sensitive content is enabled.</p>
        <p class="m-0 mb-3 text-sm text-muted">Turn it off to filter sensitive memes from discovery again.</p>
        <Button type="button" variant="secondary" size="compact" onclick={disableNsfw} disabled={nsfwPending}>{nsfwPending ? 'Saving...' : 'Turn off sensitive content'}</Button>
      {:else}
        <p class="m-0 mt-1 text-sm text-muted">Sensitive content stays hidden.</p>
        <p class="m-0 mb-3 text-sm text-muted">Use Search filters when you want to include it in results.</p>
        <ActionLink size="compact" variant="secondary" href="/search">Open Search filters</ActionLink>
      {/if}
      {#if nsfwMessage}
        <p class="m-0 mt-3 text-sm text-muted" role="status">{nsfwMessage}</p>
      {/if}
    </div>
  </Card>

  {#if data.profileStatsError}
    <Notice>{data.profileStatsError}</Notice>
  {:else if data.profileStats}
    <Card class="shadow-none">
      <details>
        <summary class="cursor-pointer text-xl font-black tracking-[-0.03em]">Interaction stats</summary>
        <p class="m-0 mt-2 text-sm text-muted">A compact summary of your recorded meme activity.</p>
        <div class="mt-4 grid gap-2 sm:grid-cols-2">
          {#each stats as stat}
            <article class="rounded-xl border border-line p-3">
              <p class="m-0 text-sm font-extrabold text-muted">{stat.label}</p>
              <p class="m-0 text-2xl font-black tracking-[-0.04em]">{stat.value}</p>
            </article>
          {/each}
        </div>
      </details>
    </Card>
  {/if}
</section>
