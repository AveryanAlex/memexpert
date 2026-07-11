<script lang="ts">
  import SeoWorkspace from '$lib/features/admin/content/seo/SeoWorkspace.svelte';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<SeoWorkspace reviews={data.reviews} paging={data.paging} loadError={data.loadError} {form} />
