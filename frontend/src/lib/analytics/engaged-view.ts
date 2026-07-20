export interface AccumulatedForegroundTimerOptions {
  durationMs?: number;
  now?: () => number;
  schedule?: (callback: () => void, delayMs: number) => ReturnType<typeof setTimeout>;
  cancel?: (timer: ReturnType<typeof setTimeout>) => void;
  onComplete: () => void;
}

export function hasEngagedViewVisibility(
  entry: Pick<IntersectionObserverEntry, 'isIntersecting' | 'intersectionRatio'>,
  minimumRatio = 0.5
): boolean {
  return entry.isIntersecting && entry.intersectionRatio >= minimumRatio;
}

export interface AccumulatedForegroundTimer {
  setVisible: (visible: boolean) => void;
  setForeground: (foreground: boolean) => void;
  dispose: () => void;
  elapsedMs: () => number;
  completed: () => boolean;
}

/** Accumulate time only while an item is sufficiently visible and the document is foregrounded. */
export function createAccumulatedForegroundTimer({
  durationMs = 3_000,
  now = () => performance.now(),
  schedule = (callback, delayMs) => setTimeout(callback, delayMs),
  cancel = (timer) => clearTimeout(timer),
  onComplete
}: AccumulatedForegroundTimerOptions): AccumulatedForegroundTimer {
  let visible = false;
  let foreground = false;
  let accumulatedMs = 0;
  let activeSince: number | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let isComplete = false;
  let disposed = false;

  const currentElapsed = () => {
    if (activeSince === null) return accumulatedMs;
    return accumulatedMs + Math.max(0, now() - activeSince);
  };

  const clearTimer = () => {
    if (timer === null) return;
    cancel(timer);
    timer = null;
  };

  const finish = () => {
    if (disposed || isComplete) return;
    accumulatedMs = Math.max(durationMs, currentElapsed());
    activeSince = null;
    isComplete = true;
    clearTimer();
    onComplete();
  };

  const armTimer = () => {
    clearTimer();
    if (disposed || isComplete || activeSince === null) return;
    const remaining = durationMs - currentElapsed();
    if (remaining <= 0) {
      finish();
      return;
    }
    timer = schedule(() => {
      timer = null;
      if (disposed || isComplete || activeSince === null) return;
      if (currentElapsed() >= durationMs) finish();
      else armTimer();
    }, remaining);
  };

  const sync = () => {
    if (disposed || isComplete) return;
    const active = visible && foreground;
    if (active && activeSince === null) {
      activeSince = now();
      armTimer();
      return;
    }
    if (!active && activeSince !== null) {
      accumulatedMs = currentElapsed();
      activeSince = null;
      clearTimer();
      if (accumulatedMs >= durationMs) finish();
    }
  };

  return {
    setVisible: (nextVisible) => {
      visible = nextVisible;
      sync();
    },
    setForeground: (nextForeground) => {
      foreground = nextForeground;
      sync();
    },
    dispose: () => {
      disposed = true;
      clearTimer();
      activeSince = null;
    },
    elapsedMs: currentElapsed,
    completed: () => isComplete
  };
}
