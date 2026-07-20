import { describe, expect, it, vi } from 'vitest';

import { ApiError } from '$lib/api/client';
import type { MemeInteractionBatchEventWrite } from '$lib/api/types';
import { createMemeInteractionQueue, INTERACTION_BATCH_MAX_EVENTS } from './interaction-queue';
import { uuidV7 } from './uuid-v7';

describe('meme interaction queue', () => {
  it('sends at most 50 events per request and preserves event order', async () => {
    const batches: string[][] = [];
    let sequence = 0;
    const queue = createMemeInteractionQueue({
      createEventId: () => `event-${++sequence}`,
      now: () => Date.parse('2026-07-20T10:00:00Z'),
      send: async (events) => batches.push(events.map((event) => event.event_id))
    });

    for (let index = 0; index < INTERACTION_BATCH_MAX_EVENTS + 1; index += 1) {
      queue.enqueue({ eventType: 'meme_impression', memeId: `meme-${index}`, attributionToken: 'signed' });
    }
    await queue.flush();
    await queue.flush();

    expect(batches).toHaveLength(2);
    expect(batches[0]).toHaveLength(50);
    expect(batches[0]?.[0]).toBe('event-1');
    expect(batches[1]).toEqual(['event-51']);
    expect(queue.pendingCount()).toBe(0);
  });

  it('retries the identical idempotency event after a failed request', async () => {
    const attempts: string[][] = [];
    const send = vi.fn(async (events: Array<{ event_id: string }>) => {
      attempts.push(events.map((event) => event.event_id));
      if (attempts.length === 1) throw new Error('offline');
    });
    const queue = createMemeInteractionQueue({
      createEventId: () => '018f22ec-9c00-7000-8000-000000000001',
      send
    });
    const eventId = queue.enqueue({
      eventType: 'meme_detail_click',
      memeId: 'meme-1',
      attributionToken: 'signed-attribution'
    });

    await queue.flush();
    expect(queue.pendingCount()).toBe(1);
    await queue.flush(true);

    expect(eventId).toBe('018f22ec-9c00-7000-8000-000000000001');
    expect(attempts).toEqual([[eventId], [eventId]]);
    expect(queue.pendingCount()).toBe(0);
  });

  it('marks a page-hide flush as keepalive', async () => {
    const keepaliveValues: boolean[] = [];
    const queue = createMemeInteractionQueue({
      createEventId: () => 'event-1',
      send: async (_events, keepalive) => keepaliveValues.push(keepalive)
    });
    queue.enqueue({ eventType: 'meme_engaged_view', memeId: 'meme-1' });

    await queue.flush(true);

    expect(keepaliveValues).toEqual([true]);
  });

  it('starts a keepalive batch while an ordinary request is still in flight', async () => {
    let releaseFirstRequest: (() => void) | undefined;
    const firstRequest = new Promise<void>((resolve) => {
      releaseFirstRequest = resolve;
    });
    const attempts: Array<{ ids: string[]; keepalive: boolean }> = [];
    let sequence = 0;
    const queue = createMemeInteractionQueue({
      createEventId: () => `event-${++sequence}`,
      flushDelayMs: 60_000,
      send: async (events, keepalive) => {
        attempts.push({ ids: events.map((event) => event.event_id), keepalive });
        if (attempts.length === 1) await firstRequest;
      }
    });
    queue.enqueue({ eventType: 'meme_impression', memeId: 'meme-1' });
    const ordinaryFlush = queue.flush(false);
    await vi.waitFor(() => expect(attempts).toHaveLength(1));

    queue.enqueue({ eventType: 'meme_detail_click', memeId: 'meme-2' });
    await queue.flush(true);

    expect(attempts[1]).toEqual({ ids: ['event-2'], keepalive: true });
    releaseFirstRequest?.();
    await ordinaryFlush;
    expect(queue.pendingCount()).toBe(0);
  });

  it('isolates a permanently invalid event instead of poisoning later telemetry', async () => {
    let sequence = 0;
    const attempts: string[][] = [];
    const queue = createMemeInteractionQueue({
      createEventId: () => `event-${++sequence}`,
      flushDelayMs: 60_000,
      send: async (events) => {
        const ids = events.map((event) => event.event_id);
        attempts.push(ids);
        if (ids.includes('event-1')) throw new ApiError(422, 'Attribution does not match the viewer.');
      }
    });
    queue.enqueue({ eventType: 'meme_impression', memeId: 'bad-meme', attributionToken: 'tampered' });
    queue.enqueue({ eventType: 'meme_impression', memeId: 'good-meme', attributionToken: 'signed' });

    await queue.flush();
    expect(attempts).toEqual([['event-1', 'event-2'], ['event-1'], ['event-2']]);
    expect(queue.pendingCount()).toBe(0);

    queue.enqueue({ eventType: 'meme_detail_click', memeId: 'later-meme', attributionToken: 'signed-later' });
    await queue.flush();
    expect(attempts.at(-1)).toEqual(['event-3']);
    expect(queue.pendingCount()).toBe(0);
  });

  it('bounds batches by encoded bytes as well as event count', async () => {
    const batches: Array<Array<{ attribution_token: string | null }>> = [];
    let sequence = 0;
    const queue = createMemeInteractionQueue({
      createEventId: () => `event-${++sequence}`,
      flushDelayMs: 60_000,
      maxBatchBytes: 700,
      send: async (events) => batches.push(events)
    });
    for (let index = 0; index < 3; index += 1) {
      queue.enqueue({
        eventType: 'meme_impression',
        memeId: `meme-${index}`,
        attributionToken: `signed-${'x'.repeat(220)}`
      });
    }

    while (queue.pendingCount() > 0) await queue.flush(true);

    expect(batches.length).toBeGreaterThan(1);
    expect(batches.flat()).toHaveLength(3);
    for (const batch of batches) {
      expect(new TextEncoder().encode(JSON.stringify({ events: batch })).byteLength).toBeLessThanOrEqual(700);
    }
  });

  it('never lets one oversized event bypass the encoded byte limit', async () => {
    const batches: MemeInteractionBatchEventWrite[][] = [];
    const queue = createMemeInteractionQueue({
      createEventId: () => 'event-1',
      flushDelayMs: 60_000,
      maxBatchBytes: 700,
      send: async (events) => batches.push(events)
    });
    queue.enqueue({
      eventType: 'meme_impression',
      memeId: 'meme-1',
      attributionToken: 'signed',
      properties: { optional_context: 'x'.repeat(2_000) }
    });

    await queue.flush();

    expect(batches).toHaveLength(1);
    expect(batches[0]?.[0]?.properties).toBeUndefined();
    expect(new TextEncoder().encode(JSON.stringify({ events: batches[0] })).byteLength).toBeLessThanOrEqual(700);
  });

  it('rotates pending viewer-bound events when authentication changes', async () => {
    const sentIds: string[] = [];
    let sequence = 0;
    const queue = createMemeInteractionQueue({
      createEventId: () => `event-${++sequence}`,
      flushDelayMs: 60_000,
      send: async (events) => sentIds.push(...events.map((event) => event.event_id))
    });
    queue.syncViewer('guest-user');
    queue.enqueue({ eventType: 'meme_impression', memeId: 'guest-meme', attributionToken: 'guest-token' });

    queue.syncViewer('full-user');
    queue.enqueue({ eventType: 'meme_impression', memeId: 'full-meme', attributionToken: 'full-token' });
    await queue.flush();

    expect(sentIds).toEqual(['event-2']);
    expect(queue.pendingCount()).toBe(0);
  });

  it('flushes with keepalive when the browser emits pagehide', async () => {
    const keepaliveValues: boolean[] = [];
    const documentTarget = new EventTarget() as Document;
    Object.defineProperty(documentTarget, 'visibilityState', { value: 'visible', configurable: true });
    const windowTarget = new EventTarget() as Window;
    const queue = createMemeInteractionQueue({
      createEventId: () => 'event-1',
      send: async (_events, keepalive) => keepaliveValues.push(keepalive)
    });
    const stop = queue.startBrowserLifecycle(documentTarget, windowTarget);
    queue.enqueue({ eventType: 'meme_impression', memeId: 'meme-1' });

    windowTarget.dispatchEvent(new Event('pagehide'));
    await vi.waitFor(() => expect(keepaliveValues).toEqual([true]));
    stop();
  });
});

describe('UUIDv7 event IDs', () => {
  it('encodes the timestamp, version, and RFC variant', () => {
    const timestamp = 1_720_000_000_000;
    const id = uuidV7(timestamp, (bytes) => bytes.fill(0xab));
    const compact = id.replaceAll('-', '');

    expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(Number.parseInt(compact.slice(0, 12), 16)).toBe(timestamp);
  });
});
