<script lang="ts">
  import { browser } from '$app/environment';
  import { cn, focusRing } from '$lib/ui/styles';
  import { Play, Volume2, VolumeX } from '@lucide/svelte';
  import { readMemeVideoCoordinator } from './meme-video-coordinator';
  import type { MemeVideoPreviewMode } from './meme-video';

  interface Props {
    src: string;
    sourceType: string;
    poster?: string | null;
    title: string;
    class?: string;
    detail?: boolean;
    previewMode?: MemeVideoPreviewMode;
    preload?: 'metadata' | 'none';
  }

  let {
    src,
    sourceType,
    poster = null,
    title,
    class: className = '',
    detail = false,
    previewMode = 'poster',
    preload = 'none'
  }: Props = $props();

  const instanceId = $props.id();
  const coordinator = readMemeVideoCoordinator();
  let containerElement = $state<HTMLElement>();
  let videoElement = $state<HTMLVideoElement>();
  let playing = $state(false);
  let muted = $state(true);
  let inViewport = $state(false);
  let hoverCapable = $state(false);
  let reducedMotion = $state(false);
  let documentVisible = $state(true);
  let userPaused = $state(false);
  let viewportAutoplayFailed = $state(false);
  let previousMode = $state<MemeVideoPreviewMode>('poster');

  const resolvedPreload = $derived(previewMode === 'viewport' ? 'metadata' : preload);

  $effect(() => {
    if (!browser || detail || !videoElement) return;
    return coordinator.register(instanceId, {
      pauseAndMute: () => pausePlayback({ mute: true, release: true }),
      mute: () => setMuted(true),
      startViewportAutoplay: () => void startPlayback({ claimPlayback: false, viewportAutoplay: true })
    });
  });

  $effect(() => {
    if (!browser || detail) return;

    const hoverQuery = window.matchMedia('(hover: hover) and (pointer: fine)');
    const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
    const syncEnvironment = () => {
      hoverCapable = hoverQuery.matches;
      reducedMotion = motionQuery.matches;
      documentVisible = !document.hidden;
    };
    const handleVisibility = () => {
      documentVisible = !document.hidden;
      if (!documentVisible) {
        coordinator.setViewportAutoplayEligible(instanceId, false);
        pausePlayback({ mute: true, release: true });
      }
    };

    syncEnvironment();
    hoverQuery.addEventListener('change', syncEnvironment);
    motionQuery.addEventListener('change', syncEnvironment);
    document.addEventListener('visibilitychange', handleVisibility);
    return () => {
      hoverQuery.removeEventListener('change', syncEnvironment);
      motionQuery.removeEventListener('change', syncEnvironment);
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  });

  $effect(() => {
    if (!browser || detail || previewMode !== 'viewport' || !containerElement || typeof IntersectionObserver === 'undefined') {
      inViewport = false;
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        const nextVisible = entries.some((entry) => entry.isIntersecting && entry.intersectionRatio >= 0.6);
        if (!nextVisible && inViewport) {
          inViewport = false;
          viewportAutoplayFailed = false;
          userPaused = false;
          coordinator.setViewportAutoplayEligible(instanceId, false);
          pausePlayback({ mute: true, release: true });
          return;
        }
        inViewport = nextVisible;
      },
      { threshold: [0, 0.6, 1] }
    );
    observer.observe(containerElement);
    return () => observer.disconnect();
  });

  $effect(() => {
    if (previewMode === previousMode) return;
    previousMode = previewMode;
    userPaused = false;
    viewportAutoplayFailed = false;
    coordinator.setViewportAutoplayEligible(instanceId, false);
    pausePlayback({ mute: true, release: true });
  });

  $effect(() => {
    if (!browser || detail) return;

    const viewportAutoplayEligible =
      previewMode === 'viewport' && inViewport && documentVisible && !reducedMotion && !userPaused && !viewportAutoplayFailed;
    coordinator.setViewportAutoplayEligible(instanceId, viewportAutoplayEligible);
    if (previewMode === 'viewport' && (!inViewport || !documentVisible || reducedMotion)) {
      pausePlayback({ mute: !inViewport || !documentVisible, release: true });
    }
  });

  async function startPlayback({ claimPlayback = true, viewportAutoplay = false } = {}) {
    if (!browser || detail || !videoElement || playing) return;
    if (claimPlayback) coordinator.activatePlayback(instanceId);
    videoElement.muted = muted;
    try {
      await videoElement.play();
    } catch {
      playing = false;
      if (viewportAutoplay) {
        viewportAutoplayFailed = true;
        coordinator.setViewportAutoplayEligible(instanceId, false);
      }
      coordinator.release(instanceId);
    }
  }

  function pausePlayback({ mute: shouldMute, release }: { mute: boolean; release: boolean }) {
    if (!videoElement) return;
    videoElement.pause();
    if (shouldMute) setMuted(true);
    if (release) coordinator.release(instanceId);
  }

  function setMuted(nextMuted: boolean) {
    muted = nextMuted;
    if (videoElement) videoElement.muted = nextMuted;
  }

  function togglePlayback() {
    if (playing) {
      userPaused = true;
      if (previewMode === 'viewport') coordinator.setViewportAutoplayEligible(instanceId, false);
      pausePlayback({ mute: false, release: true });
      return;
    }
    userPaused = false;
    viewportAutoplayFailed = false;
    void startPlayback();
  }

  function handlePointerEnter() {
    if (previewMode !== 'hover' || !hoverCapable || !documentVisible || reducedMotion) return;
    userPaused = false;
    void startPlayback();
  }

  function handlePointerLeave() {
    if (previewMode !== 'hover') return;
    userPaused = false;
    pausePlayback({ mute: true, release: true });
  }

  function handleVideoClick(event: MouseEvent) {
    event.preventDefault();
    event.stopPropagation();
    togglePlayback();
  }

  function handleVideoKeydown(event: KeyboardEvent) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    togglePlayback();
  }

  function toggleMuted(event: MouseEvent) {
    event.preventDefault();
    event.stopPropagation();
    const nextMuted = !muted;
    if (!nextMuted) coordinator.activateAudio(instanceId);
    setMuted(nextMuted);
  }
</script>

{#if detail}
  <video class={className} controls playsinline preload="metadata" {poster} aria-label={title}>
    <source {src} type={sourceType} />
  </video>
{:else}
  <div
    bind:this={containerElement}
    class="relative size-full min-w-0 overflow-hidden"
    data-video-preview-mode={previewMode}
    data-video-playing={playing}
    role="group"
    aria-label={`Video preview for ${title}`}
    onmouseenter={handlePointerEnter}
    onmouseleave={handlePointerLeave}
  >
    <video
      bind:this={videoElement}
      class={cn(className, focusRing, 'cursor-pointer outline-none')}
      muted={muted}
      playsinline
      loop
      preload={resolvedPreload}
      {poster}
      role="button"
      tabindex="0"
      aria-label={`${playing ? 'Pause' : 'Play'} ${title}`}
      aria-pressed={playing}
      onclick={handleVideoClick}
      onkeydown={handleVideoKeydown}
      onplay={() => (playing = true)}
      onpause={() => (playing = false)}
      onvolumechange={() => (muted = videoElement?.muted ?? true)}
    >
      <source {src} type={sourceType} />
    </video>

    {#if !playing}
      <span class="pointer-events-none absolute inset-0 grid place-items-center" aria-hidden="true">
        <span class="grid size-12 place-items-center rounded-full bg-ink/75 text-paper shadow-overlay backdrop-blur-sm">
          <Play class="ml-0.5 size-5 fill-current" />
        </span>
      </span>
    {/if}

    <button
      class={cn(
        'absolute bottom-3 right-3 z-10 grid size-10 place-items-center rounded-full bg-ink/75 text-paper shadow-overlay backdrop-blur-sm transition hover:bg-ink',
        focusRing
      )}
      type="button"
      aria-label={muted ? `Unmute ${title}` : `Mute ${title}`}
      aria-pressed={!muted}
      onclick={toggleMuted}
    >
      {#if muted}
        <VolumeX class="size-5" aria-hidden="true" />
      {:else}
        <Volume2 class="size-5" aria-hidden="true" />
      {/if}
    </button>
  </div>
{/if}
