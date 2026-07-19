<script lang="ts">
  import type { AdminMemeTemplateRead } from '$lib/api/types';
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import { Badge, Button, EmptyState, FormRow, Input, Label, Notice, Textarea } from '$lib/ui';
  import TemplateCard from './TemplateCard.svelte';

  let {
    templates,
    loadError,
    form
  }: {
    templates: AdminMemeTemplateRead[];
    loadError: string | null;
    form: { message?: string; error?: boolean } | null;
  } = $props();

  let query = $state('');
  const normalizedQuery = $derived(query.trim().toLowerCase());
  const filteredTemplates = $derived(
    normalizedQuery
      ? templates.filter((template) =>
          [template.name, template.slug, template.description ?? ''].some((value) => value.toLowerCase().includes(normalizedQuery))
        )
      : templates
  );
  const uncuratedCount = $derived(templates.filter((template) => !template.is_curated).length);
</script>

<section class="grid gap-3">
  <p class="m-0 text-sm font-black uppercase tracking-[0.16em] text-muted">Content · templates</p>
  <h1 class="m-0 text-[clamp(2.4rem,8vw,5rem)] font-black leading-[0.9] tracking-[-0.075em]">Meme templates</h1>
  <p class="m-0 max-w-3xl text-muted">Search the template catalog first, then open a single template when it needs curator work, a merge, or safe removal.</p>
  <div class="flex flex-wrap gap-2" aria-label="Template status summary">
    <Badge tone={uncuratedCount ? 'neutral' : 'success'}>{uncuratedCount} need curation</Badge>
    <Badge>{templates.length - uncuratedCount} curated</Badge>
  </div>
</section>

{#if form?.message}
  <Notice role={form.error ? 'alert' : undefined} tone={form.error ? 'danger' : 'success'}>{form.message}</Notice>
{/if}
{#if loadError}<Notice role="alert" tone="danger">{loadError}</Notice>{/if}

{#if !loadError}
  <section class="mt-6 grid gap-4" aria-labelledby="template-list-heading">
    <div class="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
      <div>
        <p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">Primary list</p>
        <h2 id="template-list-heading" class="m-0 text-3xl font-black tracking-[-0.05em]">Template catalog</h2>
      </div>
      <FormRow label="Search templates">
        <Input bind:value={query} type="search" placeholder="Search templates by name or slug" aria-label="Search templates by name or slug" />
      </FormRow>
    </div>
    {#if filteredTemplates.length}
      <p class="m-0 text-sm text-muted">{filteredTemplates.length} {filteredTemplates.length === 1 ? 'template' : 'templates'} shown</p>
      {#each filteredTemplates as template (template.id)}<TemplateCard {template} mergeTargets={templates} />{/each}
    {:else if templates.length}
      <EmptyState title="No templates match this search" message="Try a different name, slug, or description keyword." />
    {:else}
      <EmptyState title="No templates yet" message="Newly detected templates will appear here for curation." />
    {/if}
  </section>
{/if}

<div class="mt-6">
  <AdvancedSection title="Create a template" description="Create a new catalog entry only when an existing template does not match the meme family.">
    <form method="POST" action="?/createTemplate" class="grid gap-4">
      <div class="grid gap-4 lg:grid-cols-2">
        <FormRow label="Slug">
          <Input name="slug" required placeholder="distracted-boyfriend" />
        </FormRow>
        <FormRow label="Name">
          <Input name="name" required placeholder="Distracted Boyfriend" />
        </FormRow>
      </div>
      <FormRow label="Description (optional)">
        <Textarea name="description" rows={3} placeholder="How this template is typically used" />
      </FormRow>
      <FormRow label="Base image URL (optional)">
        <Input name="base_image_url" type="url" placeholder="https://…" />
      </FormRow>
      <Label class="!inline-flex items-center gap-2">
        <input name="is_curated" type="checkbox" />
        Curator has reviewed this template
      </Label>
      <div><Button type="submit">Create template</Button></div>
    </form>
  </AdvancedSection>
</div>
