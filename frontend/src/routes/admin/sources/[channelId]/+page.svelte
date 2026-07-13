<script lang="ts">
  import SourceDetailWorkspace from '$lib/features/admin/sources/SourceDetailWorkspace.svelte';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<SourceDetailWorkspace
  source={data.source}
  postPage={data.postPage}
  telegramAccounts={data.telegramAccounts}
  paging={data.paging}
  loadError={data.loadError}
  {form}
/>
