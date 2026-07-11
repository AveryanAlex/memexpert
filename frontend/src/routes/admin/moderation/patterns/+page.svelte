<script lang="ts">
  import BlockedPatternCard from '$lib/features/admin/moderation/BlockedPatternCard.svelte';
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import { Badge, Button, EmptyState, Input, Notice, Select, Textarea } from '$lib/ui';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();

  const activePatterns = $derived(data.patterns.filter((pattern) => pattern.is_active));
  const inactivePatterns = $derived(data.patterns.filter((pattern) => !pattern.is_active));
  const reasons = [
    ['copyright', 'Copyright'],
    ['harassment', 'Harassment'],
    ['illegal', 'Illegal content'],
    ['nsfw', 'Sensitive content'],
    ['other', 'Other'],
    ['spam', 'Spam']
  ] as const;
</script>

<section class="grid gap-3">
  <p class="m-0 text-sm font-black uppercase tracking-[0.16em] text-muted">Moderation · blocked patterns</p>
  <h1 class="m-0 text-[clamp(2.4rem,8vw,5rem)] font-black leading-[0.9] tracking-[-0.075em]">Blocked media patterns</h1>
  <p class="m-0 max-w-3xl text-muted">
    pHash compares visual fingerprints, not exact file bytes. Active patterns can catch visually similar incoming media and hold matches out of the catalog. Inactive patterns stay on record but no longer block new uploads.
  </p>
  <div class="flex flex-wrap items-center gap-2" aria-label="Pattern status summary">
    <Badge tone="success">{activePatterns.length} active</Badge>
    <Badge>{inactivePatterns.length} inactive</Badge>
  </div>
</section>

{#if form?.message}
  <Notice role={form.error ? 'alert' : undefined} tone={form.error ? 'danger' : 'success'}>{form.message}</Notice>
{/if}
{#if data.loadError}<Notice role="alert" tone="danger">{data.loadError}</Notice>{/if}

<section class="mt-6 grid gap-4" aria-labelledby="active-patterns-heading">
  <div class="flex items-end justify-between gap-3">
    <div>
      <p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">Currently enforced</p>
      <h2 id="active-patterns-heading" class="m-0 text-3xl font-black tracking-[-0.05em]">Active patterns</h2>
    </div>
    <Badge tone="success">{activePatterns.length} active</Badge>
  </div>
  {#if activePatterns.length}
    {#each activePatterns as pattern (pattern.id)}<BlockedPatternCard {pattern} />{/each}
  {:else}
    <EmptyState title="No active patterns" message="No visual fingerprints are currently blocking incoming media." />
  {/if}
</section>

<section class="mt-6 grid gap-4" aria-labelledby="inactive-patterns-heading">
  <div class="flex items-end justify-between gap-3">
    <div>
      <p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">Kept for reference</p>
      <h2 id="inactive-patterns-heading" class="m-0 text-3xl font-black tracking-[-0.05em]">Inactive patterns</h2>
    </div>
    <Badge>{inactivePatterns.length} inactive</Badge>
  </div>
  {#if inactivePatterns.length}
    {#each inactivePatterns as pattern (pattern.id)}<BlockedPatternCard {pattern} />{/each}
  {:else}
    <p class="m-0 text-muted">No inactive patterns are being kept for reference.</p>
  {/if}
</section>

<div class="mt-6">
  <AdvancedSection title="Add a blocked pattern" description="Use this only when a known visual fingerprint should be held out of future uploads.">
    <form method="POST" action="?/createBlockedPerceptualHash" class="grid gap-4">
      <label class="grid gap-2 text-sm font-extrabold">
        Reason
        <Select name="reason">
          {#each reasons as [value, label]}<option {value}>{label}</option>{/each}
        </Select>
      </label>

      <AdvancedSection title="Pattern fingerprint and match settings" description="Technical values are for exceptional policy work. Bit size is calculated from the hash when you save.">
        <div class="grid gap-4">
          <label class="grid gap-2 text-sm font-extrabold">
            Raw perceptual hash
            <Input name="perceptual_hash" autocomplete="off" spellcheck={false} placeholder="Hexadecimal visual fingerprint" />
          </label>
          <label class="grid gap-2 text-sm font-extrabold">
            Hash algorithm
            <Select name="hash_algorithm"><option value="phash">pHash</option></Select>
          </label>
          <label class="grid gap-2 text-sm font-extrabold">
            Allowed differing pHash bits
            <Input name="max_hamming_distance" type="number" min="0" step="1" value="0" />
          </label>
        </div>
      </AdvancedSection>

      <label class="inline-flex items-center gap-2 text-sm font-extrabold">
        <input name="is_active" type="checkbox" checked />
        Start blocking new uploads immediately
      </label>
      <label class="grid gap-2 text-sm font-extrabold">
        Audit note (optional)
        <Textarea name="note" placeholder="Why should this pattern be blocked?" />
      </label>
      <div><Button type="submit">Add blocked pattern</Button></div>
    </form>
  </AdvancedSection>
</div>
