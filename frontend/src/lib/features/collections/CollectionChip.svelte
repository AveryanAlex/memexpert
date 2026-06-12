<script lang="ts">
  import { cn, focusRing } from '$lib/ui/styles';

  let { href, title, meta, active = false }: { href: string; title: string; meta: string; active?: boolean } = $props();
</script>

<a
  {href}
  class={cn(
    'grid min-w-[9.5rem] gap-1 rounded-[18px] border border-line bg-paper px-4 py-3 no-underline transition hover:bg-soft',
    active && 'border-ink ring-1 ring-inset ring-ink',
    focusRing
  )}
>
  <span class="font-black">{title}</span>
  <small class="text-muted">{meta}</small>
</a>
