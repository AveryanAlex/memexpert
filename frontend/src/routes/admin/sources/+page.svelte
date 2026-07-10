<script lang="ts">
  import SourceWorkspace from '$lib/features/admin/sources/SourceWorkspace.svelte';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<SourceWorkspace sourceAdmin={data.sourceAdmin} loadError={data.loadError} {form} />
