import { describe, expect, it } from 'vitest';
import { QrRequestLifecycle } from './qr-lifecycle';

describe('QR request lifecycle', () => {
  it('aborts an in-flight request and invalidates its result when a newer request begins', () => {
    const lifecycle = new QrRequestLifecycle();
    const initial = lifecycle.begin();
    const refresh = lifecycle.begin();

    expect(initial.signal.aborted).toBe(true);
    expect(lifecycle.isCurrent(initial)).toBe(false);
    expect(refresh.signal.aborted).toBe(false);
    expect(lifecycle.isCurrent(refresh)).toBe(true);
  });

  it('aborts an in-flight request and makes its result stale on dialog close or unmount', () => {
    const lifecycle = new QrRequestLifecycle();
    const request = lifecycle.begin();

    lifecycle.cancel();

    expect(request.signal.aborted).toBe(true);
    expect(lifecycle.isCurrent(request)).toBe(false);
  });
});
