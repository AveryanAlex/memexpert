export interface QrRequestToken {
  epoch: number;
  signal: AbortSignal;
}

/** Tracks QR start/refresh requests so a closed dialog cannot accept stale responses. */
export class QrRequestLifecycle {
  #epoch = 0;
  #controller: AbortController | null = null;

  begin(): QrRequestToken {
    this.cancel();
    this.#controller = new AbortController();
    const epoch = ++this.#epoch;
    return { epoch, signal: this.#controller.signal };
  }

  cancel(): void {
    this.#epoch += 1;
    this.#controller?.abort();
    this.#controller = null;
  }

  isCurrent(token: QrRequestToken): boolean {
    return this.#epoch === token.epoch && !token.signal.aborted;
  }
}
