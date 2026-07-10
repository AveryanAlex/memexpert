import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import AdminNavigation from './AdminNavigation.svelte';
import {
  ADMIN_CATALOG_LINK,
  ADMIN_NAVIGATION_ITEMS,
  getActiveAdminNavigationItem,
  isAdminNavigationItemActive
} from './navigation';

describe('admin navigation', () => {
  it('exposes every workspace and the catalog return link', () => {
    expect(ADMIN_NAVIGATION_ITEMS.map((item) => item.label)).toEqual([
      'Overview',
      'Sources',
      'Moderation',
      'Blocked patterns',
      'SEO',
      'Templates',
      'Telegram accounts'
    ]);
    expect(ADMIN_CATALOG_LINK.label).toBe('Back to catalog');

    const { body } = render(AdminNavigation, { props: { currentPath: '/admin/sources' } });
    expect(body).toContain('aria-label="Admin navigation"');
    expect(body).toContain('Overview');
    expect(body).toContain('Sources');
    expect(body).toContain('Moderation');
    expect(body).toContain('Blocked patterns');
    expect(body).toContain('SEO');
    expect(body).toContain('Templates');
    expect(body).toContain('Telegram accounts');
    expect(body).toContain('Back to catalog');
  });

  it('uses exact matching for overview and selects one most-specific nested workspace', () => {
    const overview = ADMIN_NAVIGATION_ITEMS[0];
    const sources = ADMIN_NAVIGATION_ITEMS[1];
    const moderation = ADMIN_NAVIGATION_ITEMS[2];
    const patterns = ADMIN_NAVIGATION_ITEMS[3];

    expect(isAdminNavigationItemActive(overview, '/admin')).toBe(true);
    expect(isAdminNavigationItemActive(overview, '/admin/sources')).toBe(false);
    expect(isAdminNavigationItemActive(sources, '/admin/sources/new')).toBe(true);
    expect(isAdminNavigationItemActive(moderation, '/admin/moderation/patterns')).toBe(false);
    expect(isAdminNavigationItemActive(patterns, '/admin/moderation/patterns/edit')).toBe(true);
    expect(getActiveAdminNavigationItem('/admin/moderation/patterns')).toBe(patterns);
    expect(getActiveAdminNavigationItem('/admin/memes/123')).toBe(moderation);
    expect(isAdminNavigationItemActive(ADMIN_CATALOG_LINK, '/')).toBe(false);
  });

  it('renders one current page for a nested specialist route', () => {
    const { body } = render(AdminNavigation, { props: { currentPath: '/admin/moderation/patterns' } });

    expect(body.match(/aria-current="page"/g)).toHaveLength(1);
    expect(body).toMatch(/href="\/admin\/moderation\/patterns"[^>]*aria-current="page"/);
    expect(body).not.toMatch(/href="\/admin\/moderation"[^>]*aria-current="page"/);
  });
});
