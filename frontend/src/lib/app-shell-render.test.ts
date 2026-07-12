import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { CurrentSessionRead } from '$lib/api/types';
import AppShell from '$lib/features/app-shell/AppShell.svelte';

describe('AppShell SSR', () => {
  it('renders Discover, Search, Saved, and Account navigation with one guest sign-in action', () => {
    const { body } = render(AppShell, {
      props: {
        session: guestSession(),
        sessionError: null,
        currentPath: '/'
      }
    });

    expect(body).toContain('MemeXpert');
    expect(body).toContain('Discover');
    expect(body).toContain('href="/search"');
    expect(body).toContain('href="/library"');
    expect(body).toContain('Account');
    expect(body).toContain('Search memes');
    expect(body).toContain('aria-label="Mobile navigation"');
    expect(body).not.toContain('More filters');
    expect(body.match(/Sign in/g)).toHaveLength(1);
    expect(body).not.toContain('For You');
    expect(body).not.toContain('Trends');
    expect(body).not.toContain('Guest');
    expect(body).not.toContain('href="/admin"');

    const mobileNavigation = body.slice(body.indexOf('aria-label="Mobile navigation"'));
    expect(mobileNavigation.match(/<svg/g)).toHaveLength(4);
  });

  it('renders a single Account control for full users', () => {
    const { body } = render(AppShell, {
      props: {
        session: fullSession(),
        sessionError: null,
        currentPath: '/profile'
      }
    });

    expect(body).toContain('Account');
    expect(body).not.toContain('Sign in');
    expect(body).toContain('href="/profile"');
    expect(body.match(/href="\/profile"[^>]*aria-current="page"/g)).toHaveLength(2);
    expect(body).not.toContain('href="/admin"');
  });

  it('renders the Admin link only for admins', () => {
    const { body } = render(AppShell, {
      props: {
        session: adminSession(),
        sessionError: null,
        currentPath: '/admin'
      }
    });

    expect(body).toContain('href="/admin"');
    expect(body).toContain('>Admin</a>');
    expect(body.match(/href="\/admin"[^>]*aria-current="page"/g)).toHaveLength(2);
  });

  it('keeps Saved active on collection routes', () => {
    const { body } = render(AppShell, {
      props: {
        session: fullSession(),
        sessionError: null,
        currentPath: '/collection/example'
      }
    });

    expect(body.match(/href="\/library"[^>]*aria-current="page"/g)).toHaveLength(2);
  });
});

function guestSession(): CurrentSessionRead {
  return { user: { account_type: 'guest' }, linked_providers: { telegram_linked: false, google_linked: false, email: null } } as CurrentSessionRead;
}

function fullSession(): CurrentSessionRead {
  return { user: { account_type: 'full' }, linked_providers: { telegram_linked: true, google_linked: false, email: null } } as CurrentSessionRead;
}

function adminSession(): CurrentSessionRead {
  return {
    user: { account_type: 'full', is_admin: true },
    linked_providers: { telegram_linked: true, google_linked: false, email: null }
  } as CurrentSessionRead;
}
