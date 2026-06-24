<script lang="ts">
  import { Search, SlidersHorizontal } from '@lucide/svelte';
  import { Button, Input, Select } from '$lib/ui';
  import * as DropdownMenu from '$lib/ui/dropdown-menu';
  import { LANGUAGE_OPTIONS, MEDIA_TYPE_OPTIONS, QUICK_SEARCH_TAGS, SEARCH_SCOPE_OPTIONS } from '$lib/searchParams';

  const formId = 'global-search-form';
  let query = $state('');
</script>

<form id={formId} class="flex min-w-0 flex-1 items-center gap-2 rounded-full border border-white/10 bg-white/10 p-1.5 shadow-[inset_0_1px_0_rgb(255_255_255_/_8%)] backdrop-blur" method="GET" action="/search" role="search">
  <label class="sr-only" for="global-search-q">Search memes</label>
  <Search class="ml-3 size-4 text-slate-400" aria-hidden="true" />
  <Input id="global-search-q" class="min-w-0 flex-1 border-0 bg-transparent px-1 py-2 text-white placeholder:text-slate-400" name="q" type="search" bind:value={query} placeholder="Search memes, reactions, templates…" />

  <DropdownMenu.Root>
    <DropdownMenu.Trigger type="button" class="size-10 border-white/10 bg-white/10 text-white shadow-none" aria-label="More filters"><SlidersHorizontal class="size-4" aria-hidden="true" /></DropdownMenu.Trigger>
    <DropdownMenu.Content class="w-[min(92vw,520px)] border-white/10 bg-slate-950 p-4 text-white" align="end">
      <div class="grid gap-3 sm:grid-cols-2">
        <label class="grid gap-1 text-sm font-bold text-slate-200">Media type
          <Select form={formId} name="media_type" class="bg-slate-900 text-white"><option value="">Any type</option>{#each MEDIA_TYPE_OPTIONS as option}<option value={option.value}>{option.label}</option>{/each}</Select>
        </label>
        <label class="grid gap-1 text-sm font-bold text-slate-200">Language
          <Select form={formId} name="language" class="bg-slate-900 text-white"><option value="">Any language</option>{#each LANGUAGE_OPTIONS as option}<option value={option.value}>{option.label}</option>{/each}</Select>
        </label>
        <label class="grid gap-1 text-sm font-bold text-slate-200">NSFW
          <Select form={formId} name="include_nsfw" class="bg-slate-900 text-white"><option value="false">Hide NSFW</option><option value="true">Include NSFW</option></Select>
        </label>
        <label class="grid gap-1 text-sm font-bold text-slate-200">Scope
          <Select form={formId} name="scope" class="bg-slate-900 text-white">{#each SEARCH_SCOPE_OPTIONS as option}<option value={option.value}>{option.label}</option>{/each}</Select>
        </label>
        <label class="grid gap-1 text-sm font-bold text-slate-200 sm:col-span-2">Tags
          <Input form={formId} name="tags" class="bg-slate-900 text-white" placeholder="reaction, cat, work" />
        </label>
        <label class="grid gap-1 text-sm font-bold text-slate-200 sm:col-span-2">Collection IDs
          <Input form={formId} name="collection_ids" class="bg-slate-900 text-white" placeholder="Used with Specific collections scope" />
        </label>
      </div>
      <div class="mt-4 flex flex-wrap gap-2" aria-label="Quick filters">
        {#each QUICK_SEARCH_TAGS as tag}<a class="rounded-full bg-white/10 px-3 py-1.5 text-xs font-black text-white no-underline hover:bg-white/20" href={`/search?tags=${encodeURIComponent(tag)}&include_nsfw=false&scope=public`}>#{tag}</a>{/each}
      </div>
    </DropdownMenu.Content>
  </DropdownMenu.Root>

  <Button class="hidden rounded-full bg-white px-4 py-2 text-slate-950 hover:bg-slate-200 sm:inline-flex" type="submit">Search</Button>
</form>
