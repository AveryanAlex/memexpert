import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import AdvancedSection from './AdvancedSection.svelte';

describe('AdvancedSection', () => {
  it('renders a native disclosure collapsed by default and honors the bound open state', () => {
    const collapsed = render(AdvancedSection, { props: { title: 'Diagnostics' } }).body;
    const expanded = render(AdvancedSection, { props: { title: 'Diagnostics', open: true } }).body;

    expect(collapsed).toMatch(/<details[^>]*>/);
    expect(collapsed).not.toMatch(/<details[^>]*\sopen(?:=|\s|>)/);
    expect(expanded).toMatch(/<details[^>]*\sopen(?:=|\s|>)/);
  });
});
