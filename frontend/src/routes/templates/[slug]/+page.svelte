<script lang="ts">
  import TaxonomyLandingPage from '$lib/features/taxonomy/TaxonomyLandingPage.svelte';
  import type { PageData } from './$types';

  let { data }: { data: PageData } = $props();
</script>

<TaxonomyLandingPage kind="template" {data} />
