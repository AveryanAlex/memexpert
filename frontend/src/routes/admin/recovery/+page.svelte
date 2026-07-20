<script lang="ts">
  import RecoveryWorkspace from '$lib/features/admin/recovery/RecoveryWorkspace.svelte';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<RecoveryWorkspace
  summary={data.summary}
  workPage={data.workPage}
  jobsPage={data.jobsPage}
  filters={data.filters}
  requestIds={data.requestIds}
  loadError={data.loadError}
  jobsLoadError={data.jobsLoadError}
  {form}
/>
