<script lang="ts">
  import ModerationWorkspace from '$lib/features/admin/moderation/ModerationWorkspace.svelte';
  import type { ActionData, PageData } from './$types';

  let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<ModerationWorkspace moderation={data.moderation} loadError={data.loadError} {form} />
