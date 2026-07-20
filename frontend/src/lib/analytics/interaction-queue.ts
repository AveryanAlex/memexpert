import { ApiError, recordMemeInteractionBatch } from '$lib/api/client';
import type { MemeInteractionBatchEventType, MemeInteractionBatchEventWrite } from '$lib/api/types';
import { getContext, setContext } from 'svelte';
import { uuidV7 } from './uuid-v7';

export const INTERACTION_BATCH_MAX_EVENTS = 50;
export const INTERACTION_BATCH_MAX_BYTES = 48 * 1024;
export const interactionQueueContextKey = Symbol('meme-interaction-queue');

export interface MemeInteractionInput {
  eventType: MemeInteractionBatchEventType;
  memeId: string;
  attributionToken?: string | null;
  properties?: Record<string, unknown>;
}

export interface MemeInteractionQueue {
  enqueue: (input: MemeInteractionInput) => string;
  flush: (keepalive?: boolean) => Promise<void>;
  syncViewer: (viewerId: string | null) => void;
  startBrowserLifecycle: (documentTarget?: Document, windowTarget?: Window) => () => void;
  pendingCount: () => number;
}

interface MemeInteractionQueueOptions {
  send?: (events: MemeInteractionBatchEventWrite[], keepalive: boolean) => Promise<unknown>;
  now?: () => number;
  createEventId?: () => string;
  flushDelayMs?: number;
  retryDelayMs?: number;
  maxBatchBytes?: number;
  schedule?: (callback: () => void, delayMs: number) => ReturnType<typeof setTimeout>;
  cancel?: (timer: ReturnType<typeof setTimeout>) => void;
}

export function createMemeInteractionQueue({
  send = (events, keepalive) => recordMemeInteractionBatch({ fetch, body: { events }, keepalive }),
  now = Date.now,
  createEventId = () => uuidV7(),
  flushDelayMs = 250,
  retryDelayMs = 2_000,
  maxBatchBytes = INTERACTION_BATCH_MAX_BYTES,
  schedule = (callback, delayMs) => setTimeout(callback, delayMs),
  cancel = (timer) => clearTimeout(timer)
}: MemeInteractionQueueOptions = {}): MemeInteractionQueue {
  let pending: MemeInteractionBatchEventWrite[] = [];
  let scheduled: ReturnType<typeof setTimeout> | null = null;
  let inFlight: { generation: number; promise: Promise<void> } | null = null;
  let activeViewer: string | null | undefined;
  let generation = 0;

  const clearScheduled = () => {
    if (scheduled === null) return;
    cancel(scheduled);
    scheduled = null;
  };

  const scheduleFlush = (delayMs: number) => {
    if (scheduled !== null || pending.length === 0) return;
    scheduled = schedule(() => {
      scheduled = null;
      void flush(false);
    }, delayMs);
  };

  const runFlush = async (keepalive: boolean, batchGeneration: number): Promise<void> => {
    const batch = takeInteractionBatch(pending, maxBatchBytes);
    if (batch.length === 0) return;
    pending = pending.slice(batch.length);

    const retryEvents = await deliverInteractionBatch(batch, keepalive, send);
    if (generation !== batchGeneration) return;
    if (retryEvents.length > 0) pending = [...retryEvents, ...pending];
    if (pending.length > 0) scheduleFlush(retryEvents.length > 0 ? retryDelayMs : 0);
  };

  const flush = async (keepalive = false): Promise<void> => {
    clearScheduled();
    const flushGeneration = generation;

    // A page-hide delivery must start synchronously even when an ordinary
    // request is still in flight. Its batch is dequeued independently, so it
    // cannot duplicate the active request and the browser receives a real
    // keepalive fetch before the document is discarded.
    if (keepalive) {
      await runFlush(true, flushGeneration);
      return;
    }

    const currentFlight = inFlight;
    if (currentFlight?.generation === flushGeneration) {
      await currentFlight.promise;
      if (generation === flushGeneration && pending.length > 0) scheduleFlush(0);
      return;
    }
    if (pending.length === 0) return;

    const flight = {
      generation: flushGeneration,
      promise: runFlush(false, flushGeneration)
    };
    inFlight = flight;
    await flight.promise;
    if (inFlight === flight) inFlight = null;
  };

  return {
    enqueue: ({ eventType, memeId, attributionToken = null, properties }) => {
      const eventId = createEventId();
      const event = boundInteractionEvent({
        event_id: eventId,
        event_type: eventType,
        meme_id: memeId,
        occurred_at: new Date(now()).toISOString(),
        attribution_token: attributionToken,
        ...(properties && Object.keys(properties).length > 0 ? { properties } : {})
      }, maxBatchBytes);
      if (!event) return eventId;
      pending.push(event);
      scheduleFlush(pending.length >= INTERACTION_BATCH_MAX_EVENTS ? 0 : flushDelayMs);
      return eventId;
    },
    flush,
    syncViewer: (viewerId) => {
      if (activeViewer === undefined) {
        activeViewer = viewerId;
        return;
      }
      if (activeViewer === viewerId) return;

      // Attribution is viewer-bound. Once authentication rotates, sending an
      // old viewer's queued token with the new cookie can only fail (and risks
      // cross-viewer retention), so rotate the queue explicitly.
      activeViewer = viewerId;
      generation += 1;
      clearScheduled();
      pending = [];
    },
    startBrowserLifecycle: (documentTarget = document, windowTarget = window) => {
      const flushOnHide = () => {
        if (documentTarget.visibilityState === 'hidden') void flush(true);
      };
      const flushOnPageHide = () => void flush(true);
      documentTarget.addEventListener('visibilitychange', flushOnHide);
      windowTarget.addEventListener('pagehide', flushOnPageHide);

      return () => {
        documentTarget.removeEventListener('visibilitychange', flushOnHide);
        windowTarget.removeEventListener('pagehide', flushOnPageHide);
        void flush(true);
      };
    },
    pendingCount: () => pending.length
  };
}

export function provideMemeInteractionQueue(): MemeInteractionQueue {
  const queue = createMemeInteractionQueue();
  setContext(interactionQueueContextKey, queue);
  return queue;
}

const fallbackQueue: MemeInteractionQueue = {
  enqueue: () => '',
  flush: async () => undefined,
  syncViewer: () => undefined,
  startBrowserLifecycle: () => () => undefined,
  pendingCount: () => 0
};

export function readMemeInteractionQueue(): MemeInteractionQueue {
  return getContext<MemeInteractionQueue | undefined>(interactionQueueContextKey) ?? fallbackQueue;
}

function takeInteractionBatch(
  events: MemeInteractionBatchEventWrite[],
  maxBatchBytes: number
): MemeInteractionBatchEventWrite[] {
  const batch: MemeInteractionBatchEventWrite[] = [];
  for (const event of events.slice(0, INTERACTION_BATCH_MAX_EVENTS)) {
    const candidate = [...batch, event];
    if (batch.length > 0 && interactionBatchByteLength(candidate) > maxBatchBytes) break;
    batch.push(event);
  }
  return batch;
}

function boundInteractionEvent(
  event: MemeInteractionBatchEventWrite,
  maxBatchBytes: number
): MemeInteractionBatchEventWrite | null {
  if (interactionBatchByteLength([event]) <= maxBatchBytes) return event;

  // Product interaction fields and signed attribution are authoritative;
  // optional client properties must never make one event exceed the browser's
  // bounded keepalive budget. If the required fields alone are still too
  // large, discard the malformed event instead of sending an oversized body.
  if (event.properties !== undefined) {
    const { properties: _properties, ...withoutProperties } = event;
    if (interactionBatchByteLength([withoutProperties]) <= maxBatchBytes) {
      return withoutProperties;
    }
  }
  return null;
}

async function deliverInteractionBatch(
  events: MemeInteractionBatchEventWrite[],
  keepalive: boolean,
  send: NonNullable<MemeInteractionQueueOptions['send']>
): Promise<MemeInteractionBatchEventWrite[]> {
  try {
    await send(events, keepalive);
    return [];
  } catch (error) {
    if (isRetryableInteractionError(error)) return events;

    // A permanent client error must not poison every later event. During a
    // normal flush, split the rejected transaction until only the invalid
    // event is discarded; valid siblings retain their UUIDs and are accepted.
    // Page-hide work stays to one bounded keepalive request because browsers
    // enforce a shared keepalive byte budget during unload.
    if (keepalive || events.length <= 1) return [];
    const midpoint = Math.ceil(events.length / 2);
    return [
      ...(await deliverInteractionBatch(events.slice(0, midpoint), false, send)),
      ...(await deliverInteractionBatch(events.slice(midpoint), false, send))
    ];
  }
}

function isRetryableInteractionError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  return error.status === 408 || error.status === 425 || error.status === 429 || error.status >= 500;
}

function interactionBatchByteLength(events: MemeInteractionBatchEventWrite[]): number {
  return new TextEncoder().encode(JSON.stringify({ events })).byteLength;
}
