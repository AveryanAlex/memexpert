import { createRawSnippet } from 'svelte';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import Masonry from './Masonry.svelte';
import type { MasonryItemLayout } from './types';

describe('Masonry', () => {
  it('server-renders a pending flat list in item order and forwards root attributes', () => {
    const { body } = render(Masonry, {
      props: {
        items: ['rank-one', 'rank-two', 'rank-three'],
        getKey: (item: unknown) => String(item),
        element: 'section',
        role: 'list',
        'aria-label': 'Ranked results',
        busy: true,
        class: 'custom-grid',
        children: createRawSnippet<[unknown, number, MasonryItemLayout]>((item, index, layout) => ({
          render: () =>
            `<article data-rendered-index="${index()}" data-rendered-columns="${layout().columnCount}">${String(item())}</article>`
        }))
      }
    });

    expect(body).toContain('<section');
    expect(body).toContain('role="list"');
    expect(body).toContain('aria-label="Ranked results"');
    expect(body).toContain('aria-busy="true"');
    expect(body).toContain('data-layout="masonry"');
    expect(body).toContain('data-column-count="1"');
    expect(body).toContain('data-masonry-state="pending"');
    expect(body).toContain('custom-grid');
    expect(body.match(/data-masonry-index=/g)).toHaveLength(3);
    expect(body.indexOf('rank-one')).toBeLessThan(body.indexOf('rank-two'));
    expect(body.indexOf('rank-two')).toBeLessThan(body.indexOf('rank-three'));
    expect(body).toContain('data-rendered-columns="1"');
  });
});
