<script lang="ts">
  import type { AdminMemeSeoReviewRowRead } from '$lib/api/types';
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import AdminMediaPreview from '$lib/features/admin/moderation/AdminMediaPreview.svelte';
  import { Badge, Button, Card, FormRow, Input, Textarea } from '$lib/ui';

  let { review, pageNumber }: { review: AdminMemeSeoReviewRowRead; pageNumber: number } = $props();

  const status = $derived(
    review.status === 'missing' ? 'Needs SEO' : review.status === 'edited' ? 'Manually edited' : 'Generated'
  );
  const statusTone = $derived(review.status === 'edited' ? 'success' : 'neutral');
  const page = $derived(review.seo_page);
  const summary = $derived(
    page?.meta_description || page?.caption || page?.body_text || 'No SEO page has been created for this meme yet.'
  );
</script>

<Card class="m-0 grid gap-5 p-5">
  <div class="grid gap-5 lg:grid-cols-[180px_minmax(0,1fr)]">
    <AdminMediaPreview meme={review.meme} compact label="SEO review meme preview" />
    <div class="grid content-start gap-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="grid gap-1">
          <p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">SEO queue item</p>
          <h3 class="m-0 text-2xl font-black tracking-[-0.04em]">{page?.page_title ?? 'No search title yet'}</h3>
        </div>
        <Badge tone={statusTone}>{status}</Badge>
      </div>
      <p class="m-0 max-w-4xl text-sm text-muted">{summary}</p>
      <a class="w-fit text-sm font-black underline decoration-2 underline-offset-4" href={`/admin/memes/${review.meme.id}`}>Review meme</a>
    </div>
  </div>

  <dl class="m-0 grid gap-3 text-sm sm:grid-cols-2 xl:grid-cols-4">
    <div><dt class="font-extrabold text-muted">Media</dt><dd class="m-0 capitalize">{review.meme.media_type} · {review.meme.language}</dd></div>
    <div><dt class="font-extrabold text-muted">Catalog tags</dt><dd class="m-0">{review.meme.tags.length ? review.meme.tags.join(', ') : 'No tags'}</dd></div>
    <div><dt class="font-extrabold text-muted">SEO tags</dt><dd class="m-0">{page?.tags.length ? page.tags.join(', ') : 'No SEO tags'}</dd></div>
    <div><dt class="font-extrabold text-muted">Last updated</dt><dd class="m-0"><time datetime={page?.edited_at ?? page?.generated_at ?? review.meme.updated_at}>{formatAdminTimestamp(page?.edited_at ?? page?.generated_at ?? review.meme.updated_at)}</time></dd></div>
  </dl>

  <AdvancedSection title="Edit SEO details" description="Review or write the search details for this one meme. These fields are not published until saved.">
    <form method="POST" action={`?page=${pageNumber}&/updateSeoPage`} class="grid gap-4">
      <input type="hidden" name="meme_id" value={review.meme.id} />
      <div class="grid gap-4 lg:grid-cols-2">
        <FormRow label="Search URL slug">
          <Input name="slug" value={page?.slug ?? ''} required maxlength={255} />
        </FormRow>
        <FormRow label="Search page title">
          <Input name="page_title" value={page?.page_title ?? ''} required maxlength={255} />
        </FormRow>
      </div>
      <FormRow label="Search description">
        <Textarea name="meta_description" value={page?.meta_description ?? ''} required rows={3} />
      </FormRow>
      <FormRow label="Image alt text">
        <Textarea name="alt_text" value={page?.alt_text ?? ''} required rows={2} />
      </FormRow>
      <div class="grid gap-4 lg:grid-cols-2">
        <FormRow label="Caption (optional)">
          <Input name="caption" value={page?.caption ?? ''} />
        </FormRow>
        <FormRow label="SEO tags (also updates catalog tags)" hint="Saving SEO tags also updates the catalog tags for this meme.">
          <Input name="tags" value={page?.tags.join(', ') ?? review.meme.tags.join(', ')} placeholder="reaction, launch" />
        </FormRow>
      </div>
      <FormRow label="Search body copy (optional)">
        <Textarea name="body_text" value={page?.body_text ?? ''} rows={5} />
      </FormRow>
      <div><Button type="submit" variant="secondary">Save SEO details</Button></div>
    </form>
  </AdvancedSection>

  <AdvancedSection title="Regenerate and overwrite SEO" description="Generated output can replace SEO text and catalog tags, reassign this meme's template, and create an uncurated template. Manual edits are cleared." danger>
    <form method="POST" action={`?page=${pageNumber}&/regenerateSeoPage`} class="grid gap-3">
      <input type="hidden" name="meme_id" value={review.meme.id} />
      <FormRow label="Type REGENERATE to confirm">
        <Input name="confirmation_phrase" autocomplete="off" required />
      </FormRow>
      <div><Button type="submit" variant="danger">Regenerate and overwrite SEO</Button></div>
    </form>
  </AdvancedSection>
</Card>
