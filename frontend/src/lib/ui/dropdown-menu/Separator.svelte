<script lang="ts">
  import { DropdownMenu } from 'bits-ui';
  import { cn } from '../styles';

  let { class: className = '', ...rest }: { class?: string; [key: string]: unknown } = $props();
</script>

<DropdownMenu.Separator {...rest} class={cn('m-1 h-px bg-line', className)} />
