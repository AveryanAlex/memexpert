<script lang="ts">
  import EngagementAnalytics from '$lib/features/admin/analytics/EngagementAnalytics.svelte';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();
</script>

<EngagementAnalytics
  dashboard={data.dashboard}
  searchQueries={data.searchQueries}
  queryDetail={data.queryDetail}
  selectedQueryKey={data.selectedQueryKey}
  offset={data.offset}
  sort={data.sort}
  requestedRange={data.requestedRange}
  loadError={data.loadError}
/>
