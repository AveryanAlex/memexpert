<script lang="ts">
  import { page } from '$app/state';
  import { consumerPageViewSurface, recordPageView } from './pageView';

  let lastPathname: string | null = null;

  $effect(() => {
    const pathname = page.url.pathname;
    if (pathname === lastPathname) return;
    lastPathname = pathname;

    const surface = consumerPageViewSurface(pathname);
    if (surface) void recordPageView(surface);
  });
</script>
