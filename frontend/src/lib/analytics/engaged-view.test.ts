import { afterEach, describe, expect, it, vi } from 'vitest';

import { createAccumulatedForegroundTimer, hasEngagedViewVisibility } from './engaged-view';

afterEach(() => vi.useRealTimers());

describe('accumulated foreground engagement', () => {
  it('requires at least 50 percent actual visibility', () => {
    expect(hasEngagedViewVisibility({ isIntersecting: true, intersectionRatio: 0.499 })).toBe(false);
    expect(hasEngagedViewVisibility({ isIntersecting: false, intersectionRatio: 1 })).toBe(false);
    expect(hasEngagedViewVisibility({ isIntersecting: true, intersectionRatio: 0.5 })).toBe(true);
  });

  it('completes once after three visible foreground seconds', () => {
    vi.useFakeTimers();
    let now = 0;
    const complete = vi.fn();
    const timer = createAccumulatedForegroundTimer({ now: () => now, onComplete: complete });

    timer.setForeground(true);
    timer.setVisible(true);
    advance(2_999);
    expect(complete).not.toHaveBeenCalled();
    advance(1);
    expect(complete).toHaveBeenCalledOnce();
    advance(10_000);
    expect(complete).toHaveBeenCalledOnce();

    function advance(milliseconds: number) {
      now += milliseconds;
      vi.advanceTimersByTime(milliseconds);
    }
  });

  it('pauses while the document is backgrounded and accumulates the remaining time', () => {
    vi.useFakeTimers();
    let now = 0;
    const complete = vi.fn();
    const timer = createAccumulatedForegroundTimer({ now: () => now, onComplete: complete });

    timer.setVisible(true);
    timer.setForeground(true);
    advance(2_000);
    timer.setForeground(false);
    advance(30_000);
    expect(timer.elapsedMs()).toBe(2_000);
    expect(complete).not.toHaveBeenCalled();
    timer.setForeground(true);
    advance(1_000);
    expect(complete).toHaveBeenCalledOnce();

    function advance(milliseconds: number) {
      now += milliseconds;
      vi.advanceTimersByTime(milliseconds);
    }
  });

  it('pauses below the visibility threshold and does not fire after disposal', () => {
    vi.useFakeTimers();
    let now = 0;
    const complete = vi.fn();
    const timer = createAccumulatedForegroundTimer({ now: () => now, onComplete: complete });

    timer.setForeground(true);
    timer.setVisible(true);
    advance(1_500);
    timer.setVisible(false);
    advance(10_000);
    timer.setVisible(true);
    timer.dispose();
    advance(10_000);
    expect(complete).not.toHaveBeenCalled();

    function advance(milliseconds: number) {
      now += milliseconds;
      vi.advanceTimersByTime(milliseconds);
    }
  });
});
