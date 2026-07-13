import { render } from 'svelte/server';
import { describe, expect, it, vi } from 'vitest';

import TelegramLoginModal from './TelegramLoginModal.svelte';

describe('TelegramLoginModal', () => {
  it('does not start browser authentication requests during SSR', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    try {
      const { body } = render(TelegramLoginModal, { props: { open: true } });

      expect(body).toEqual(expect.any(String));
      expect(fetchMock).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
