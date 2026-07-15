import { describe, expect, it } from 'vitest';

import { aggregateAnalyticsCategories, formatMetricChange } from './format';

describe('admin analytics formatting', () => {
  it('preserves the sign of a negative metric change', () => {
    expect(
      formatMetricChange({ value: 5, previous_value: 10, change: -5, change_percent: -50 })
    ).toBe('-5 (-50%) vs. prior period');
  });

  it('combines overflow categories into Other without losing their counts', () => {
    const result = aggregateAnalyticsCategories(
      [
        { key: 'alpha', count: 10 },
        { key: 'beta', count: 9 },
        { key: 'gamma', count: 8 },
        { key: 'delta', count: 7 },
        { key: 'epsilon', count: 6 },
        { key: 'zeta', count: 5 },
        { key: 'eta', count: 4 }
      ],
      6
    );

    expect(result).toEqual({
      items: [
        { label: 'Alpha', count: 10 },
        { label: 'Beta', count: 9 },
        { label: 'Gamma', count: 8 },
        { label: 'Delta', count: 7 },
        { label: 'Epsilon', count: 6 },
        { label: 'Other', count: 9 }
      ],
      aggregated: true
    });
    expect(result.items.reduce((sum, item) => sum + item.count, 0)).toBe(49);
  });
});
