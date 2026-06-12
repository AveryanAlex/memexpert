<script lang="ts">
  import { LoaderCircle } from '@lucide/svelte';

  let { label = 'Loading' }: { label?: string } = $props();
</script>

<div class="flex items-center gap-2 rounded-2xl border border-line bg-paper px-4 py-3 text-muted" role="status">
  <LoaderCircle class="size-4 animate-spin" aria-hidden="true" />
  <span>{label}</span>
</div>
