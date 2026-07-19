import { createRawSnippet } from 'svelte';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import PillLink from './PillLink.svelte';

describe('PillLink', () => {
  it('forwards anchor props and marks an active compact link as the current page', () => {
    const { body } = render(PillLink, {
      props: {
        active: true,
        size: 'compact',
        href: '/trends',
        target: '_blank',
        rel: 'noreferrer',
        id: 'trend-pill',
        class: 'custom-pill',
        children: createRawSnippet(() => ({ render: () => '<span>Trending</span>' }))
      }
    });

    expect(body).toContain('href="/trends"');
    expect(body).toContain('target="_blank"');
    expect(body).toContain('rel="noreferrer"');
    expect(body).toContain('id="trend-pill"');
    expect(body).toContain('aria-current="page"');
    expect(body).toContain('focus-visible:outline-accent');
    expect(body).toContain('border-ink bg-ink text-paper');
    expect(body).toContain('px-3 py-1.5 text-sm');
    expect(body).toContain('custom-pill');
    expect(body).toContain('<span>Trending</span>');
  });

  it('preserves an explicit aria-current value on an inactive link', () => {
    const { body } = render(PillLink, {
      props: {
        href: '#year',
        'aria-current': 'step'
      }
    });

    expect(body).toContain('aria-current="step"');
    expect(body).toContain('border-line bg-paper text-ink');
    expect(body).not.toContain('aria-current="page"');
  });
});
