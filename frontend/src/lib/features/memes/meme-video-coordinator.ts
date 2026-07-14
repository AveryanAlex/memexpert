import { getContext, setContext } from 'svelte';

export interface MemeVideoController {
  pauseAndMute: () => void;
  mute: () => void;
  startViewportAutoplay: () => void;
}

export interface MemeVideoCoordinator {
  register: (id: string, controller: MemeVideoController) => () => void;
  activatePlayback: (id: string) => void;
  activateAudio: (id: string) => void;
  setViewportAutoplayEligible: (id: string, eligible: boolean) => void;
  release: (id: string) => void;
}

export const memeVideoCoordinatorContextKey = Symbol('meme-video-coordinator');

export function createMemeVideoCoordinator(): MemeVideoCoordinator {
  const controllers = new Map<string, MemeVideoController>();
  const viewportAutoplayCandidates = new Set<string>();
  let activePlaybackId: string | null = null;
  let activeAudioId: string | null = null;

  const resumeViewportAutoplay = () => {
    if (activePlaybackId !== null) return;

    for (const id of viewportAutoplayCandidates) {
      const controller = controllers.get(id);
      if (!controller) continue;

      // Reserve playback before invoking the controller. Its play attempt is async,
      // and another candidate must not start while that attempt is pending.
      activePlaybackId = id;
      controller.startViewportAutoplay();
      return;
    }
  };

  return {
    register: (id, controller) => {
      controllers.set(id, controller);
      resumeViewportAutoplay();
      return () => {
        controllers.delete(id);
        viewportAutoplayCandidates.delete(id);
        if (activePlaybackId === id) activePlaybackId = null;
        if (activeAudioId === id) activeAudioId = null;
        resumeViewportAutoplay();
      };
    },
    activatePlayback: (id) => {
      if (activePlaybackId === id) return;

      const previousPlaybackId = activePlaybackId;
      // Claim playback first so a release from the previous controller cannot
      // immediately resume a viewport candidate during this handoff.
      activePlaybackId = id;
      if (previousPlaybackId) controllers.get(previousPlaybackId)?.pauseAndMute();
    },
    activateAudio: (id) => {
      if (activeAudioId && activeAudioId !== id) {
        controllers.get(activeAudioId)?.mute();
      }
      activeAudioId = id;
    },
    setViewportAutoplayEligible: (id, eligible) => {
      if (eligible) {
        viewportAutoplayCandidates.add(id);
      } else {
        viewportAutoplayCandidates.delete(id);
        if (activePlaybackId === id) activePlaybackId = null;
        if (activeAudioId === id) activeAudioId = null;
      }
      resumeViewportAutoplay();
    },
    release: (id) => {
      if (activePlaybackId === id) activePlaybackId = null;
      if (activeAudioId === id) activeAudioId = null;
      resumeViewportAutoplay();
    }
  };
}

export function provideMemeVideoCoordinator(): MemeVideoCoordinator {
  const coordinator = createMemeVideoCoordinator();
  setContext(memeVideoCoordinatorContextKey, coordinator);
  return coordinator;
}

export function readMemeVideoCoordinator(): MemeVideoCoordinator {
  return getContext<MemeVideoCoordinator | undefined>(memeVideoCoordinatorContextKey) ?? createMemeVideoCoordinator();
}
