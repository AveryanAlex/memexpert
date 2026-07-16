<script lang="ts">
  import type {
    SourceInventorySortDirection,
    SourceInventorySortKey
  } from './view-model';
  import { sourceInventoryDefaultDirection } from './view-model';

  let {
    label,
    sortKey,
    activeKey,
    direction,
    class: className = '',
    onSort
  }: {
    label: string;
    sortKey: SourceInventorySortKey;
    activeKey: SourceInventorySortKey;
    direction: SourceInventorySortDirection;
    class?: string;
    onSort: (key: SourceInventorySortKey) => void;
  } = $props();

  const active = $derived(sortKey === activeKey);
  const ariaSort = $derived(active ? direction : 'none');
  const nextDirection = $derived(
    active
      ? direction === 'ascending' ? 'descending' : 'ascending'
      : sourceInventoryDefaultDirection(sortKey)
  );
</script>

<th scope="col" aria-sort={ariaSort} class="px-3 py-3 font-black {className}">
  <button
    type="button"
    class="inline-flex items-center gap-1 rounded-lg text-left text-inherit underline-offset-4 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
    aria-label={`Sort by ${label}, ${nextDirection}`}
    onclick={() => onSort(sortKey)}
  >
    <span>{label}</span>
    <span aria-hidden="true" class="text-xs">{active ? (direction === 'ascending' ? '↑' : '↓') : '↕'}</span>
  </button>
</th>
