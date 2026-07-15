<script lang="ts">
  import OverviewAnalytics from '$lib/features/admin/analytics/OverviewAnalytics.svelte';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();
</script>

<OverviewAnalytics dashboard={data.dashboard} requestedRange={data.requestedRange} loadError={data.loadError} />
