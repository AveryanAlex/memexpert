<script lang="ts">
  import RecoveryBatchDetail from '$lib/features/admin/recovery/RecoveryBatchDetail.svelte';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<RecoveryBatchDetail
  batch={data.batch}
  itemsPage={data.itemsPage}
  itemFilters={data.itemFilters}
  retryFailedRequestId={data.retryFailedRequestId}
  loadError={data.loadError}
  itemsLoadError={data.itemsLoadError}
  {form}
/>
