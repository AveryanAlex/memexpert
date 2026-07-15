<script lang="ts">
  import type { AdminRecoveryCapability, AdminRecoveryWorkKind } from '$lib/api/types';
  import { Button, FormRow, Input, Textarea } from '$lib/ui';
  import { recoveryCapabilityLabel } from './view-model';

  let {
    kind,
    workId,
    version,
    requestId,
    capability,
    action = '?/retryRecoveryWork',
    compact = false
  }: {
    kind: AdminRecoveryWorkKind;
    workId: string;
    version: string;
    requestId: string;
    capability: AdminRecoveryCapability;
    action?: string;
    compact?: boolean;
  } = $props();

  const label = $derived(recoveryCapabilityLabel(capability));
</script>

<details class="rounded-2xl border border-line bg-soft p-3" data-recovery-action={capability}>
  <summary class="cursor-pointer text-sm font-black text-ink">{label}</summary>
  <form method="POST" {action} class="mt-3 grid gap-3">
    <input type="hidden" name="kind" value={kind} />
    <input type="hidden" name="work_id" value={workId} />
    <input type="hidden" name="version" value={version} />
    <input type="hidden" name="request_id" value={requestId} />
    <input type="hidden" name="capability" value={capability} />
    <FormRow label="Audit reason" hint="Recorded with the recovery request.">
      <Textarea
        name="reason"
        rows={compact ? 2 : 3}
        minlength={3}
        maxlength={500}
        placeholder="Why is this recovery safe now?"
        required
      />
    </FormRow>
    {#if capability === 'archive_dead_letter'}
      <FormRow label="Type ARCHIVE" hint="Archiving resolves the item without replaying it.">
        <Input name="confirmation_phrase" autocomplete="off" required />
      </FormRow>
    {/if}
    <Button type="submit" size={compact ? 'compact' : 'md'} variant={capability === 'archive_dead_letter' ? 'danger' : 'primary'}>
      {label}
    </Button>
  </form>
</details>
