<script lang="ts">
  import ContentAnalytics from '$lib/features/admin/analytics/ContentAnalytics.svelte';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();
</script>

<ContentAnalytics dashboard={data.dashboard} requestedRange={data.requestedRange} loadError={data.loadError} />
