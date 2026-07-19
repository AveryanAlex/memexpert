<script lang="ts">
  import type { AdminMemeTemplateRead } from '$lib/api/types';
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import { Badge, Button, Card, FormRow, Input, Label, Select, Textarea } from '$lib/ui';

  let { template, mergeTargets }: { template: AdminMemeTemplateRead; mergeTargets: AdminMemeTemplateRead[] } = $props();

  const targetTemplates = $derived(mergeTargets.filter((candidate) => candidate.id !== template.id));
</script>

<Card class="m-0 grid gap-5 p-5">
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div class="grid gap-1">
      <p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">Template · {template.slug}</p>
      <h3 class="m-0 text-2xl font-black tracking-[-0.04em]">{template.name}</h3>
    </div>
    <Badge tone={template.is_curated ? 'success' : 'neutral'}>{template.is_curated ? 'Curated' : 'Needs curation'}</Badge>
  </div>

  <p class="m-0 text-sm text-muted">{template.description ?? 'No curator description yet.'}</p>

  <dl class="m-0 grid gap-3 text-sm sm:grid-cols-2 xl:grid-cols-4">
    <div><dt class="font-extrabold text-muted">Slug</dt><dd class="m-0">{template.slug}</dd></div>
    <div><dt class="font-extrabold text-muted">Base image</dt><dd class="m-0">{template.base_image_url ? 'Available' : 'Not set'}</dd></div>
    <div><dt class="font-extrabold text-muted">Text regions</dt><dd class="m-0">{template.text_regions?.length ?? 0} mapped</dd></div>
    <div><dt class="font-extrabold text-muted">Last updated</dt><dd class="m-0"><time datetime={template.updated_at}>{formatAdminTimestamp(template.updated_at)}</time></dd></div>
  </dl>

  <AdvancedSection title="Edit template" description="Update the curator-facing name, description, and reference image for this template.">
    <form method="POST" action="?/updateTemplate" class="grid gap-4">
      <input type="hidden" name="template_id" value={template.id} />
      <div class="grid gap-4 lg:grid-cols-2">
        <FormRow label="Slug">
          <Input name="slug" value={template.slug} required />
        </FormRow>
        <FormRow label="Name">
          <Input name="name" value={template.name} required />
        </FormRow>
      </div>
      <FormRow label="Description (optional)">
        <Textarea name="description" value={template.description ?? ''} rows={3} />
      </FormRow>
      <FormRow label="Base image URL (optional)">
        <Input name="base_image_url" type="url" value={template.base_image_url ?? ''} />
      </FormRow>
      <Label class="!inline-flex items-center gap-2">
        <input name="is_curated" type="checkbox" checked={template.is_curated} />
        Curator has reviewed this template
      </Label>
      <div><Button type="submit" variant="secondary">Save template</Button></div>
    </form>
  </AdvancedSection>

  <AdvancedSection title="Merge or delete template" description="Merging reassigns affected memes to another template. Deletion is only available when this template is no longer referenced." danger>
    <div class="grid gap-6">
      <form method="POST" action="?/mergeTemplate" class="grid gap-3 border-b border-danger-line pb-6">
        <input type="hidden" name="template_id" value={template.id} />
        <FormRow label="Merge into">
          <Select name="target_template_id" required disabled={!targetTemplates.length}>
            <option value="">Choose a target template</option>
            {#each targetTemplates as target (target.id)}<option value={target.id}>{target.name} · {target.slug}</option>{/each}
          </Select>
        </FormRow>
        <FormRow label="Reason for merge" hint="This reason accompanies the affected meme decisions created by the merge.">
          <Textarea name="note" required placeholder="Why are these templates being combined?" rows={2} />
        </FormRow>
        <FormRow label="Type MERGE to confirm">
          <Input name="confirmation_phrase" autocomplete="off" required />
        </FormRow>
        <div><Button type="submit" variant="danger" disabled={!targetTemplates.length}>Merge template</Button></div>
      </form>

      <form method="POST" action="?/deleteTemplate" class="grid gap-3">
        <input type="hidden" name="template_id" value={template.id} />
        <FormRow label="Type DELETE to confirm">
          <Input name="confirmation_phrase" autocomplete="off" required />
        </FormRow>
        <div><Button type="submit" variant="danger">Delete template</Button></div>
      </form>
    </div>
  </AdvancedSection>
</Card>
