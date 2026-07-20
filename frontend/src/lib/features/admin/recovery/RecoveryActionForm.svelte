<script lang="ts">
  import { untrack } from 'svelte';
  import type {
    AdminRecoveryActionCandidateRead,
    AdminRecoveryReplayScope,
    AdminRecoveryRetryLimit,
    AdminRecoveryWorkKind
  } from '$lib/api/types';
  import { Button, FormRow, Input, Label, Notice, Select, Textarea } from '$lib/ui';
  import * as Dialog from '$lib/ui/dialog';
  import {
    recoveryCapabilityLabel,
    recoveryAcknowledgement,
    recoveryActionRequirements,
    recoveryDefaultRetryLimit,
    recoveryDefaultScope,
    recoverySafeMessage,
    normalizeRecoveryAction
  } from './view-model';

  let {
    kind,
    workId,
    version,
    requestId,
    actionCandidate,
    stage = null,
    action = '?/actRecoveryWork',
    compact = false
  }: {
    kind: AdminRecoveryWorkKind;
    workId: string;
    version: string;
    requestId: string;
    actionCandidate: AdminRecoveryActionCandidateRead;
    stage?: string | null;
    action?: string;
    compact?: boolean;
  } = $props();

  const candidate = $derived(normalizeRecoveryAction(actionCandidate));
  let selectedScope = $state<AdminRecoveryReplayScope>(untrack(() => recoveryDefaultScope(actionCandidate)));
  let retryLimit = $state<AdminRecoveryRetryLimit>(untrack(() => recoveryDefaultRetryLimit(actionCandidate)));
  const scopeRequirements = $derived(recoveryActionRequirements(candidate, selectedScope));
  const label = $derived(recoveryCapabilityLabel(candidate.capability));
  const dialogId = $derived(`recovery-action-${safeId(workId)}-${candidate.capability}`);

  function safeId(value: string): string {
    return value.replace(/[^a-zA-Z0-9_-]/g, '-');
  }
</script>

{#if !candidate.available}
  <div class="grid gap-2 rounded-2xl border border-line bg-soft p-3" data-recovery-action={candidate.capability} data-recovery-action-blocked>
    <strong class="text-sm text-ink">{label}</strong>
    {#if candidate.blocked_prerequisites?.length}
      <ul class="m-0 grid gap-1 pl-5 text-xs text-muted">
        {#each candidate.blocked_prerequisites as prerequisite}<li>{recoverySafeMessage(prerequisite)}</li>{/each}
      </ul>
    {:else}
      <p class="m-0 text-xs text-muted">This action is not eligible in the current canonical state.</p>
    {/if}
  </div>
{:else}
  <Dialog.Root>
    <Dialog.Trigger
      type="button"
      class={compact
        ? '!rounded-[14px] !bg-ink !px-3 !py-2 !text-sm'
        : '!rounded-[16px] !bg-accent !px-4 !py-2.5 !text-on-accent'}
      data-recovery-action={candidate.capability}
    >{label}</Dialog.Trigger>
    <Dialog.Content aria-labelledby={`${dialogId}-title`} aria-describedby={`${dialogId}-description`} class="max-h-[90dvh] overflow-y-auto">
      <div class="grid gap-1">
        <Dialog.Title id={`${dialogId}-title`} class="m-0 text-2xl font-black tracking-[-0.04em]">{label}</Dialog.Title>
        <Dialog.Description id={`${dialogId}-description`} class="m-0 text-sm text-muted">
          {stage ? `Selected stage: ${stage.replaceAll('_', ' ')}.` : 'Review the backend-declared action before scheduling it.'}
        </Dialog.Description>
      </div>

      {#if candidate.downstream_stages?.length}
        <div class="rounded-2xl border border-line bg-soft p-4">
          <strong>Downstream stages</strong>
          <p class="mb-0 mt-1 text-sm text-muted">{candidate.downstream_stages.join(' → ')}</p>
        </div>
      {/if}

      {#if candidate.capability === 'regenerate_derivatives'}
        <Notice>Replaces the web video and matching poster atomically. OCR, embeddings, classification, and search synchronization are not rerun.</Notice>
      {/if}

      {#if (candidate.capability === 'replay_stage' || candidate.capability === 'retry_stage') && selectedScope === 'stage_only'}
        <Notice class="border-line bg-cream">Stage-only replay leaves existing dependents untouched. Their data may be stale until deliberately replayed.</Notice>
      {:else if (candidate.capability === 'replay_stage' || candidate.capability === 'retry_stage') && selectedScope === 'stage_and_dependents'}
        <Notice>Cascading replay follows the displayed dependency chain. Search targets may run concurrently after classification.</Notice>
      {/if}

      {#if scopeRequirements.warnings.length || scopeRequirements.risks.length}
        <div class="grid gap-2 rounded-2xl border border-danger-line bg-danger-surface p-4 text-sm">
          <strong>Risks and warnings</strong>
          <ul class="m-0 grid gap-1 pl-5">
            {#each scopeRequirements.warnings as warning}<li>{warning}</li>{/each}
            {#each scopeRequirements.risks as risk}<li>{recoverySafeMessage(risk)}</li>{/each}
          </ul>
        </div>
      {/if}

      <form method="POST" {action} class="grid gap-4">
        <input type="hidden" name="kind" value={kind} />
        <input type="hidden" name="work_id" value={workId} />
        <input type="hidden" name="version" value={version} />
        <input type="hidden" name="request_id" value={requestId} />
        <input type="hidden" name="action" value={candidate.capability} />

        <div class="grid gap-3 sm:grid-cols-2">
          <FormRow label="Replay scope">
            <Select name="scope" bind:value={selectedScope}>
              {#each candidate.scopes ?? ['stage_only'] as scope}
                <option value={scope}>{scope === 'stage_only' ? 'Selected stage only' : 'Stage and dependents'}</option>
              {/each}
            </Select>
          </FormRow>
          <FormRow label="Retry limit" hint="Only retryable failures consume this budget.">
            <Select name="retry_limit" bind:value={retryLimit}>
              {#each candidate.retry_limits ?? [1, 3, 5] as limit}<option value={limit}>{limit} attempt{limit === 1 ? '' : 's'}</option>{/each}
            </Select>
          </FormRow>
        </div>

        <FormRow label="Audit reason" hint="Recorded with the replay or repair request.">
          <Textarea
            name="reason"
            rows={compact ? 2 : 3}
            minlength={3}
            maxlength={500}
            placeholder="Why is this action safe now?"
            required
          />
        </FormRow>

        {#each scopeRequirements.required_acknowledgements as acknowledgement, index (`${acknowledgement.key}:${index}`)}
          {@const acknowledgementRead = recoveryAcknowledgement(acknowledgement)}
          <Label class="!flex items-start gap-2 rounded-2xl border border-line bg-soft p-3">
            <input type="checkbox" name="acknowledgement" value={acknowledgementRead.key} required class="mt-1 size-4 accent-accent" />
            <span class="text-sm">{acknowledgementRead.label}</span>
          </Label>
        {/each}

        {#if candidate.capability === 'archive_dead_letter'}
          <FormRow label="Type ARCHIVE" hint="Archiving resolves the item without replaying it.">
            <Input name="confirmation_phrase" autocomplete="off" required />
          </FormRow>
        {/if}

        <div class="flex flex-wrap justify-end gap-2">
          <Dialog.Close class="rounded-[14px] border border-line bg-paper px-4 py-2 font-semibold text-ink hover:bg-soft">Cancel</Dialog.Close>
          <Button type="submit" variant={candidate.capability === 'archive_dead_letter' ? 'danger' : 'primary'}>{label}</Button>
        </div>
      </form>
    </Dialog.Content>
  </Dialog.Root>
{/if}
