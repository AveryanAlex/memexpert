<script lang="ts">
  import RecoveryBatchDetail from '$lib/features/admin/recovery/RecoveryBatchDetail.svelte';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<RecoveryBatchDetail batch={data.batch} loadError={data.loadError} {form} />
