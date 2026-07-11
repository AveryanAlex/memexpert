<script lang="ts">
  import TemplateWorkspace from '$lib/features/admin/content/templates/TemplateWorkspace.svelte';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<TemplateWorkspace templates={data.templates} loadError={data.loadError} {form} />
