import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { CurrentSessionRead } from '$lib/api/types';
import AppShell from '$lib/features/app-shell/AppShell.svelte';

describe('AppShell SSR', () => {
  it('renders desktop navigation, global search, and mobile tabs for guests', () => {
    const { body } = render(AppShell, {
      props: {
        session: guestSession(),
        sessionError: null,
        currentPath: '/'
      }
    });

    expect(body).toContain('MemeXpert');
    expect(body).toContain('For You');
    expect(body).toContain('Trends');
    expect(body).toContain('Profile');
    expect(body).toContain('Search memes');
    expect(body).toContain('More filters');
    expect(body).toContain('Sign in');
    expect(body).toContain('Guest');
    expect(body).toContain('aria-label="Mobile navigation"');
  });

  it('renders connected profile state for full users', () => {
    const { body } = render(AppShell, {
      props: {
        session: fullSession(),
        sessionError: null,
        currentPath: '/profile'
      }
    });

    expect(body).toContain('Full profile');
    expect(body).toContain('Connected: Telegram');
    expect(body).toContain('href="/profile"');
  });
});

function guestSession(): CurrentSessionRead {
  return { user: { account_type: 'guest' }, linked_providers: { telegram_linked: false, google_linked: false, email: null } } as CurrentSessionRead;
}

function fullSession(): CurrentSessionRead {
  return { user: { account_type: 'full' }, linked_providers: { telegram_linked: true, google_linked: false, email: null } } as CurrentSessionRead;
}
