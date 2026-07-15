<script lang="ts">
  import RecoveryWorkDetail from '$lib/features/admin/recovery/RecoveryWorkDetail.svelte';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<RecoveryWorkDetail work={data.work} requestId={data.requestId} loadError={data.loadError} {form} />
