<script lang="ts">
  import type { PublicMemeCardRead } from '$lib/api/types';
  import MemeCard from './MemeCard.svelte';

  let { memes, label = 'Meme results' }: { memes: PublicMemeCardRead[]; label?: string } = $props();
</script>

<section class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4" aria-label={label}>
  {#each memes as meme (meme.id)}
    <MemeCard {meme} />
  {/each}
</section>
