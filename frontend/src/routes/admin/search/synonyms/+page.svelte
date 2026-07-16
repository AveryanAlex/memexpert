<script lang="ts">
  import SearchSynonymWorkspace from '$lib/features/admin/search-synonyms/SearchSynonymWorkspace.svelte';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<SearchSynonymWorkspace
  catalogs={data.catalogs}
  sync={data.sync}
  requestIds={data.requestIds}
  loadedAt={data.loadedAt}
  loadError={data.loadError}
  {form}
/>
