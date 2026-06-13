import { getContext, setContext } from 'svelte';

import type { CurrentSessionRead } from '$lib/api/types';

export interface ViewerCapabilities {
  canPinMemes: boolean;
}

export type ViewerCapabilitiesReader = () => ViewerCapabilities;

export const viewerCapabilitiesContextKey = Symbol('viewer-capabilities');

const guestViewerCapabilities: ViewerCapabilities = Object.freeze({
  canPinMemes: false
});

export function viewerCapabilitiesFromSession(session: CurrentSessionRead | null | undefined): ViewerCapabilities {
  return {
    canPinMemes: session?.user.account_type === 'full'
  };
}

export function provideViewerCapabilities(readCapabilities: ViewerCapabilitiesReader): void {
  setContext(viewerCapabilitiesContextKey, readCapabilities);
}

export function readViewerCapabilities(): ViewerCapabilitiesReader {
  return getContext<ViewerCapabilitiesReader | undefined>(viewerCapabilitiesContextKey) ?? (() => guestViewerCapabilities);
}
