<script lang="ts">
  import AudienceAnalytics from '$lib/features/admin/analytics/AudienceAnalytics.svelte';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();
</script>

<AudienceAnalytics dashboard={data.dashboard} requestedRange={data.requestedRange} loadError={data.loadError} />
