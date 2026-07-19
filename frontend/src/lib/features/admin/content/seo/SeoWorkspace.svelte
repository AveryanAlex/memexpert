<script lang="ts">
  import type { AdminMemeSeoReviewRowRead } from '$lib/api/types';
  import { ActionLink, Badge, EmptyState, Notice } from '$lib/ui';
  import SeoReviewCard from './SeoReviewCard.svelte';

  let {
    reviews,
    paging,
    loadError,
    form
  }: {
    reviews: AdminMemeSeoReviewRowRead[];
    paging: { page: number; pageSize: number; hasPrevious: boolean; hasNext: boolean };
    loadError: string | null;
    form: { message?: string; error?: boolean } | null;
  } = $props();

  const missingCount = $derived(reviews.filter((review) => review.status === 'missing').length);
  const editedCount = $derived(reviews.filter((review) => review.status === 'edited').length);
</script>

<section class="grid gap-3">
  <p class="m-0 text-sm font-black uppercase tracking-[0.16em] text-muted">Content · SEO</p>
  <h1 class="m-0 text-[clamp(2.4rem,8vw,5rem)] font-black leading-[0.9] tracking-[-0.075em]">SEO review queue</h1>
  <p class="m-0 max-w-3xl text-muted">Start with public, safe memes that need search details. Review the current text before opening an editor or overwriting it with a new generation.</p>
  <div class="flex flex-wrap gap-2" aria-label="SEO queue status summary">
    <Badge tone="neutral">{missingCount} need SEO</Badge>
    <Badge tone={editedCount ? 'success' : 'neutral'}>{editedCount} manually edited</Badge>
    <Badge>{reviews.length - missingCount - editedCount} generated</Badge>
  </div>
</section>

{#if form?.message}
  <Notice role={form.error ? 'alert' : undefined} tone={form.error ? 'danger' : 'success'}>{form.message}</Notice>
{/if}
{#if loadError}<Notice role="alert" tone="danger">{loadError}</Notice>{/if}

{#if !loadError}
  <section class="mt-6 grid gap-4" aria-labelledby="seo-review-list-heading">
    <div class="flex items-end justify-between gap-3">
      <div>
        <p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">Primary queue</p>
        <h2 id="seo-review-list-heading" class="m-0 text-3xl font-black tracking-[-0.05em]">SEO pages to review</h2>
      </div>
      <Badge>{reviews.length} shown</Badge>
    </div>
    {#if reviews.length}
      {#each reviews as review (review.meme.id)}<SeoReviewCard {review} pageNumber={paging.page} />{/each}
    {:else if paging.page > 1}
      <EmptyState title="No SEO pages on this page" message="There are no items on this page. Go back to the previous page to continue reviewing the queue." />
    {:else}
      <EmptyState title="No SEO pages need review" message="Public, safe memes will appear here when they are ready for search details." />
    {/if}

    <nav class="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4" aria-label="SEO review pagination">
      {#if paging.hasPrevious}
        <ActionLink variant="secondary" size="compact" href={`/admin/content/seo?page=${paging.page - 1}`}>Previous</ActionLink>
      {:else}
        <span class="rounded-[14px] border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">Previous</span>
      {/if}
      <span class="text-sm font-extrabold text-muted">Page {paging.page}</span>
      {#if paging.hasNext}
        <ActionLink variant="secondary" size="compact" href={`/admin/content/seo?page=${paging.page + 1}`}>Next</ActionLink>
      {:else}
        <span class="rounded-[14px] border border-line bg-soft px-3 py-2 text-sm font-extrabold text-muted" aria-disabled="true">Next</span>
      {/if}
    </nav>
  </section>
{/if}
