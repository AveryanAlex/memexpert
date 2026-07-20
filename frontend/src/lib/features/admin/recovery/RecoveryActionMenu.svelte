<script lang="ts">
  import type { AdminRecoveryActionCandidateRead, AdminRecoveryWorkKind } from '$lib/api/types';
  import RecoveryActionForm from './RecoveryActionForm.svelte';

  let {
    kind,
    workId,
    version,
    requestId,
    actions,
    stage = null,
    formAction = '?/actRecoveryWork',
    compact = false
  }: {
    kind: AdminRecoveryWorkKind;
    workId: string;
    version: string;
    requestId: string;
    actions: AdminRecoveryActionCandidateRead[];
    stage?: string | null;
    formAction?: string;
    compact?: boolean;
  } = $props();
</script>

{#if actions.length}
  <details class="rounded-2xl border border-line bg-soft p-3" data-recovery-action-menu>
    <summary class="cursor-pointer text-sm font-black text-ink">Actions ({actions.length})</summary>
    <div class="mt-3 grid gap-2">
      {#each actions as actionCandidate, index (`${actionCandidate.capability}:${index}`)}
        <RecoveryActionForm
          {kind}
          {workId}
          {version}
          {requestId}
          {actionCandidate}
          {stage}
          action={formAction}
          {compact}
        />
      {/each}
    </div>
  </details>
{/if}
