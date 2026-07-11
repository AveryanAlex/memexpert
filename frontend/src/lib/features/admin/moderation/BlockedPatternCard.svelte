<script lang="ts">
  import type { AdminBlockedPerceptualHashRead, ModerationReason } from '$lib/api/types';
  import AdvancedSection from '$lib/features/admin/AdvancedSection.svelte';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import { Badge, Button, Input, Select, Textarea } from '$lib/ui';

  let { pattern }: { pattern: AdminBlockedPerceptualHashRead } = $props();

  const reasons: Array<[ModerationReason, string]> = [
    ['copyright', 'Copyright'],
    ['harassment', 'Harassment'],
    ['illegal', 'Illegal content'],
    ['nsfw', 'Sensitive content'],
    ['other', 'Other'],
    ['spam', 'Spam']
  ];

  const statusLabel = $derived(pattern.is_active ? 'Active' : 'Inactive');
  const matchTolerance = $derived(
    pattern.max_hamming_distance === 0
      ? 'Exact pHash match'
      : `Up to ${pattern.max_hamming_distance} differing pHash ${pattern.max_hamming_distance === 1 ? 'bit' : 'bits'}`
  );
</script>

<article class="grid gap-4 rounded-3xl border border-line bg-paper p-5 shadow-[0_12px_32px_rgb(64_46_26_/_6%)]">
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div>
      <p class="m-0 text-xs font-black uppercase tracking-[0.14em] text-muted">{pattern.reason} policy pattern</p>
      <h3 class="mb-0 mt-1 text-2xl font-black tracking-[-0.04em]">{pattern.reason === 'nsfw' ? 'Sensitive media' : pattern.reason.replace(/^./, (letter) => letter.toUpperCase())}</h3>
    </div>
    <Badge tone={pattern.is_active ? 'success' : 'neutral'}>{statusLabel}</Badge>
  </div>

  <dl class="m-0 grid gap-3 text-sm sm:grid-cols-2 xl:grid-cols-4">
    <div><dt class="font-extrabold text-muted">Reason</dt><dd class="m-0">{pattern.reason.replaceAll('_', ' ')}</dd></div>
    <div><dt class="font-extrabold text-muted">State</dt><dd class="m-0">{statusLabel}</dd></div>
    <div><dt class="font-extrabold text-muted">Match tolerance</dt><dd class="m-0">{matchTolerance}</dd></div>
    <div><dt class="font-extrabold text-muted">Updated</dt><dd class="m-0"><time datetime={pattern.updated_at}>{formatAdminTimestamp(pattern.updated_at)}</time></dd></div>
  </dl>

  <AdvancedSection title="Pattern details and editing" description="Raw fingerprint values and audit context are available for exceptional review.">
    <div class="grid gap-4">
      <dl class="m-0 grid gap-3 text-sm sm:grid-cols-2">
        <div><dt class="font-extrabold text-muted">Raw perceptual hash</dt><dd class="m-0 break-all font-mono text-xs">{pattern.perceptual_hash}</dd></div>
        <div><dt class="font-extrabold text-muted">Hash algorithm</dt><dd class="m-0">{pattern.hash_algorithm}</dd></div>
        <div><dt class="font-extrabold text-muted">Bit size</dt><dd class="m-0">{pattern.hash_size} bits</dd></div>
        <div><dt class="font-extrabold text-muted">Maximum differing pHash bits</dt><dd class="m-0">{pattern.max_hamming_distance}</dd></div>
      </dl>

      <form method="POST" action="?/updateBlockedPerceptualHash" class="grid gap-4 rounded-2xl border border-line bg-cream/50 p-4">
        <input type="hidden" name="blocked_hash_id" value={pattern.id} />
        {#if pattern.is_active}<input type="hidden" name="is_active" value="on" />{/if}
        <label class="grid gap-2 text-sm font-extrabold">
          Raw perceptual hash
          <Input name="perceptual_hash" autocomplete="off" spellcheck={false} value={pattern.perceptual_hash} />
        </label>
        <div class="grid gap-4 sm:grid-cols-2">
          <label class="grid gap-2 text-sm font-extrabold">
            Reason
            <Select name="reason" value={pattern.reason}>
              {#each reasons as [value, label]}<option {value}>{label}</option>{/each}
            </Select>
          </label>
          <label class="grid gap-2 text-sm font-extrabold">
            Hash algorithm
            <Select name="hash_algorithm" value={pattern.hash_algorithm}><option value="phash">pHash</option></Select>
          </label>
          <label class="grid gap-2 text-sm font-extrabold">
            Allowed differing pHash bits
            <Input name="max_hamming_distance" type="number" min="0" step="1" value={String(pattern.max_hamming_distance)} />
          </label>
          <div class="grid content-end gap-1 text-sm"><strong>Bit size</strong><span class="text-muted">{pattern.hash_size} bits, derived from the raw hash</span></div>
        </div>
        <label class="grid gap-2 text-sm font-extrabold">
          Audit note (optional)
          <Textarea name="note" value={pattern.note ?? ''} placeholder="Why is this pattern changing?" />
        </label>
        <div><Button type="submit" variant="secondary">Save pattern details</Button></div>
      </form>
    </div>
  </AdvancedSection>

  <AdvancedSection title="Pattern lifecycle and deletion" description="Deactivating stops new matches. Deleting preserves audit history and will deactivate instead if quarantined files still reference this pattern." danger>
    <div class="grid gap-5">
      {#if pattern.is_active}
        <form method="POST" action="?/deactivateBlockedPerceptualHash" class="grid gap-3">
          <input type="hidden" name="blocked_hash_id" value={pattern.id} />
          <label class="grid gap-2 text-sm font-extrabold">
            Type DEACTIVATE to confirm
            <Input name="confirmation_phrase" autocomplete="off" required />
          </label>
          <label class="grid gap-2 text-sm font-extrabold">
            Audit note (optional)
            <Textarea name="note" placeholder="Why should this stop matching?" />
          </label>
          <div><Button type="submit" variant="danger">Deactivate pattern</Button></div>
        </form>
      {:else}
        <p class="m-0 text-sm text-muted">This pattern is inactive and does not block new uploads.</p>
        <form method="POST" action="?/reactivateBlockedPerceptualHash" class="grid gap-3">
          <input type="hidden" name="blocked_hash_id" value={pattern.id} />
          <label class="grid gap-2 text-sm font-extrabold">
            Type REACTIVATE to confirm
            <Input name="confirmation_phrase" autocomplete="off" required />
          </label>
          <div><Button type="submit" variant="danger">Reactivate pattern</Button></div>
        </form>
      {/if}

      <form method="POST" action="?/deleteBlockedPerceptualHash" class="grid gap-3 border-t border-danger-line pt-5">
        <input type="hidden" name="blocked_hash_id" value={pattern.id} />
        <label class="grid gap-2 text-sm font-extrabold">
          Type DELETE to confirm
          <Input name="confirmation_phrase" autocomplete="off" required />
        </label>
        <div><Button type="submit" variant="danger">Delete pattern</Button></div>
      </form>
    </div>
  </AdvancedSection>
</article>
