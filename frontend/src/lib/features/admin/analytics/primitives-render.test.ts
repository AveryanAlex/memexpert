import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import AnalyticsBreakdownChart from './AnalyticsBreakdownChart.svelte';
import AnalyticsDonut from './AnalyticsDonut.svelte';
import AnalyticsFunnel from './AnalyticsFunnel.svelte';

describe('admin analytics primitives', () => {
  it('renders a zero-width fill for zero-value funnel stages', () => {
    const { body } = render(AnalyticsFunnel, {
      props: {
        funnel: {
          searches: 10,
          searches_with_results: 0,
          searches_without_results: 10,
          detail_clicks: 0,
          downloads: 0
        }
      }
    });

    expect(body.match(/style="width: 0%;"/g)).toHaveLength(3);
  });

  it('discloses overflow aggregation in donut and breakdown views', () => {
    const items = Array.from({ length: 10 }, (_, index) => ({ key: `category_${index + 1}`, count: 10 - index }));
    const donutBody = render(AnalyticsDonut, { props: { items, label: 'Surface mix' } }).body;
    const breakdownBody = render(AnalyticsBreakdownChart, {
      props: { items, label: 'Languages', description: 'Catalog languages.' }
    }).body;

    expect(donutBody).toContain('Other');
    expect(donutBody).toContain('percentages use the full total');
    expect(breakdownBody).toContain('Other');
    expect(breakdownBody).toContain('Smaller categories are combined into Other.');
  });
});
