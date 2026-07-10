import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { PageData } from '../routes/admin/$types';
import AdminOverviewPage from '../routes/admin/+page.svelte';

describe('/admin overview page', () => {
  it('renders linked attention cards and health summaries without legacy forms', () => {
    const { body } = render(AdminOverviewPage, { props: { data: pageData() } });

    expect(body).toContain('What needs attention?');
    expect(body).toContain('Open reports');
    expect(body).toContain('Sources need attention');
    expect(body).toContain('Telegram accounts need attention');
    expect(body).toContain('Missing SEO');
    expect(body).toContain('Templates to curate');
    expect(body).toContain('2 need an account · 3 stale · 4 pending suggestions');
    expect(body).toContain('1 waiting · 5 healthy');
    expect(body).toContain('6 ready');
    expect(body).toContain('href="/admin/moderation"');
    expect(body).toContain('href="/admin/sources"');
    expect(body).toContain('href="/admin/telegram"');
    expect(body).toContain('href="/admin/content/seo"');
    expect(body).toContain('href="/admin/content/templates"');
    expect(body).not.toContain('action="?/addSourceChannel"');
    expect(body).not.toContain('action="?/createTemplate"');
    expect(body).not.toContain('Channel Suggestions');
    expect(body).not.toContain('Moderation Pattern Controls');
  });

  it('renders overview load failures as an alert', () => {
    const { body } = render(AdminOverviewPage, {
      props: { data: { ...pageData(), loadError: 'Could not load admin tools.' } }
    });

    expect(body).toContain('role="alert"');
    expect(body).toContain('Could not load admin tools.');
  });
});

function pageData(): PageData {
  return {
    session: null,
    sessionError: null,
    adminUser: { id: 'admin-1', email: 'admin@example.test' },
    overview: {
      open_report_count: 7,
      pending_suggestion_count: 4,
      source_attention_count: 5,
      orphaned_source_count: 2,
      stale_source_count: 3,
      waiting_source_count: 1,
      healthy_source_count: 5,
      telegram_account_attention_count: 8,
      ready_telegram_account_count: 6,
      missing_seo_count: 9,
      uncurated_template_count: 10
    },
    loadError: null
  } as PageData;
}
