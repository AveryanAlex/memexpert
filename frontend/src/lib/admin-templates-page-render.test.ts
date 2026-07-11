import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { AdminMemeTemplateRead } from '$lib/api/types';
import TemplatesPage from '../routes/admin/content/templates/+page.svelte';

describe('/admin/content/templates page', () => {
  it('renders a searchable template list before collapsed create, edit, and shared danger controls', () => {
    const source = template({ id: '11111111-1111-4111-8111-111111111111', is_curated: false });
    const target = template({
      id: '22222222-2222-4222-8222-222222222222',
      name: 'Distracted partner',
      slug: 'distracted-partner',
      is_curated: true,
      updated_at: '2026-07-10T23:45:30-04:00'
    });
    const { body } = render(TemplatesPage, {
      props: { data: { templates: [source, target], loadError: null }, form: null } as never
    });

    expect(body).toContain('Meme templates');
    expect(body).toContain('Template catalog');
    expect(body).toContain('Search templates');
    expect(body).toContain('placeholder="Search templates by name or slug"');
    expect(body).toContain('Distracted partner');
    expect(body).toContain('Needs curation');
    expect(body).toContain('Curated');
    expect(body).toContain('Base image');
    expect(body).toContain('Text regions');
    expect(body).toContain('2026-07-11 03:45 UTC');
    expect(body).toContain('Create a template');
    expect(body).toContain('Edit template');
    expect(body).toContain('Merge or delete template');
    expect(body).toContain('Type MERGE to confirm');
    expect(body).toContain('Type DELETE to confirm');
    expect(body).toContain('Reason for merge');
    expect(body).toContain('This reason accompanies the affected meme decisions created by the merge.');
    expect(body).not.toContain('Audit note (optional)');
    expect(body).not.toContain('Why is this unused template being removed?');
    expect(body).toContain('action="?/createTemplate"');
    expect(body).toContain('action="?/updateTemplate"');
    expect(body).toContain('action="?/mergeTemplate"');
    expect(body).toContain('action="?/deleteTemplate"');
    expect(body).not.toContain(`${source.id} to confirm`);
    expect(body).not.toContain('Paste the template ID');
    expect(body).not.toMatch(/<details[^>]*\sopen(?:=|\s|>)/);
  });

  it('renders action and load errors without a successful empty state', () => {
    const empty = render(TemplatesPage, {
      props: {
        data: { templates: [], loadError: 'Could not load meme templates.' },
        form: { message: 'Type DELETE to confirm this action.', error: true }
      } as never
    }).body;

    expect(empty.match(/role="alert"/g)).toHaveLength(2);
    expect(empty).toContain('Could not load meme templates.');
    expect(empty).toContain('Type DELETE to confirm this action.');
    expect(empty).not.toContain('No templates yet');
    expect(empty).not.toContain('No templates match this search');
  });
});

function template(overrides: Partial<AdminMemeTemplateRead> = {}): AdminMemeTemplateRead {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    slug: 'distracted-boyfriend',
    name: 'Distracted boyfriend',
    description: 'A person distracted by a new option.',
    is_curated: false,
    base_image_url: 'https://images.memexpert.test/distracted.jpg',
    text_regions: [{ x: 0.1, y: 0.2 }],
    created_at: '2026-07-10T10:00:00Z',
    updated_at: '2026-07-10T11:00:00Z',
    ...overrides
  };
}
