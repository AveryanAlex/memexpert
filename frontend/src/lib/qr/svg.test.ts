import { describe, expect, it } from 'vitest';

import { qrSvg, qrSvgDataUri } from './svg';

describe('QR SVG renderer', () => {
  it('renders a deterministic SVG QR code for a Telegram login URL', () => {
    const first = qrSvg('tg://login?token=fake-qr-token');
    const second = qrSvg('tg://login?token=fake-qr-token');

    expect(first).toEqual(second);
    expect(first).toContain('<svg');
    expect(first).toContain('role="img"');
    expect(first).toContain('<title>Telegram login QR</title>');
    expect(first).toContain('<path d="');
    expect(first).not.toContain('tg://login?token=fake-qr-token');
  });

  it('escapes the accessible title', () => {
    const svg = qrSvg('tg://login?token=fake-qr-token', { title: 'Scan <QR> & login' });

    expect(svg).toContain('<title>Scan &lt;QR&gt; &amp; login</title>');
    expect(svg).not.toContain('<title>Scan <QR> & login</title>');
  });

  it('returns a data URI suitable for img src attributes', () => {
    const uri = qrSvgDataUri('tg://login?token=fake-qr-token');

    expect(uri.startsWith('data:image/svg+xml;utf8,')).toBe(true);
    expect(decodeURIComponent(uri.replace('data:image/svg+xml;utf8,', ''))).toContain('<svg');
  });
});
