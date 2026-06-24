interface QrVersionSpec {
  version: number;
  size: number;
  dataCodewords: number;
  eccCodewordsPerBlock: number;
  blocks: number;
}

export interface QrSvgOptions {
  title?: string;
  border?: number;
}

const QR_VERSION_SPECS: QrVersionSpec[] = [
  { version: 1, size: 21, dataCodewords: 19, eccCodewordsPerBlock: 7, blocks: 1 },
  { version: 2, size: 25, dataCodewords: 34, eccCodewordsPerBlock: 10, blocks: 1 },
  { version: 3, size: 29, dataCodewords: 55, eccCodewordsPerBlock: 15, blocks: 1 },
  { version: 4, size: 33, dataCodewords: 80, eccCodewordsPerBlock: 20, blocks: 1 },
  { version: 5, size: 37, dataCodewords: 108, eccCodewordsPerBlock: 26, blocks: 1 },
  { version: 6, size: 41, dataCodewords: 136, eccCodewordsPerBlock: 18, blocks: 2 }
];

const FORMAT_ECC_LEVEL_L = 1;
const FORMAT_MASK_PATTERN = 0;
const encoder = new TextEncoder();

const gfExp: number[] = new Array(512).fill(0);
const gfLog: number[] = new Array(256).fill(0);
let gfValue = 1;
for (let i = 0; i < 255; i += 1) {
  gfExp[i] = gfValue;
  gfLog[gfValue] = i;
  gfValue <<= 1;
  if ((gfValue & 0x100) !== 0) {
    gfValue ^= 0x11d;
  }
}
for (let i = 255; i < gfExp.length; i += 1) {
  gfExp[i] = gfExp[i - 255];
}

const generatorCache = new Map<number, number[]>();

export function qrSvg(value: string, options: QrSvgOptions = {}): string {
  const matrix = createQrMatrix(value);
  const border = options.border ?? 4;
  const title = escapeXml(options.title ?? 'Telegram login QR');
  const viewBoxSize = matrix.length + border * 2;
  const darkPath = matrixToPath(matrix, border);

  return [
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${viewBoxSize} ${viewBoxSize}" role="img" aria-label="${title}">`,
    `<title>${title}</title>`,
    '<rect width="100%" height="100%" fill="#fff"/>',
    `<path d="${darkPath}" fill="#111827"/>`,
    '</svg>'
  ].join('');
}

export function qrSvgDataUri(value: string, options: QrSvgOptions = {}): string {
  return `data:image/svg+xml;utf8,${encodeURIComponent(qrSvg(value, options))}`;
}

function createQrMatrix(value: string): boolean[][] {
  if (!value.trim()) {
    throw new Error('QR payload must not be blank.');
  }

  const payload = Array.from(encoder.encode(value));
  const spec = chooseVersionSpec(payload.length);
  const { modules, functionModules } = createBaseMatrix(spec);
  const codewords = addErrorCorrection(encodePayload(payload, spec), spec);
  drawCodewords(modules, functionModules, codewords);
  applyMask(modules, functionModules);
  drawFormatBits(modules, spec.size, FORMAT_MASK_PATTERN);

  return modules.map((row) => row.map(Boolean));
}

function chooseVersionSpec(payloadLength: number): QrVersionSpec {
  const spec = QR_VERSION_SPECS.find((candidate) => payloadLength <= maxBytePayload(candidate));
  if (spec) {
    return spec;
  }
  const maxLength = maxBytePayload(QR_VERSION_SPECS[QR_VERSION_SPECS.length - 1]);
  throw new Error(`QR payload is too long (${payloadLength} bytes; max ${maxLength} bytes).`);
}

function maxBytePayload(spec: QrVersionSpec): number {
  return Math.floor((spec.dataCodewords * 8 - 12) / 8);
}

function encodePayload(payload: number[], spec: QrVersionSpec): number[] {
  const bits: number[] = [];
  appendBits(bits, 0b0100, 4);
  appendBits(bits, payload.length, 8);
  for (const byte of payload) {
    appendBits(bits, byte, 8);
  }

  const capacityBits = spec.dataCodewords * 8;
  appendBits(bits, 0, Math.min(4, capacityBits - bits.length));
  while (bits.length % 8 !== 0) {
    bits.push(0);
  }

  const data: number[] = [];
  for (let i = 0; i < bits.length; i += 8) {
    data.push(bitsToByte(bits.slice(i, i + 8)));
  }
  for (let pad = 0xec; data.length < spec.dataCodewords; pad ^= 0xfd) {
    data.push(pad);
  }
  return data;
}

function appendBits(target: number[], value: number, length: number): void {
  for (let i = length - 1; i >= 0; i -= 1) {
    target.push((value >>> i) & 1);
  }
}

function bitsToByte(bits: number[]): number {
  return bits.reduce((acc, bit) => (acc << 1) | bit, 0);
}

function addErrorCorrection(data: number[], spec: QrVersionSpec): number[] {
  const blocks = splitBlocks(data, spec.blocks);
  const eccBlocks = blocks.map((block) => reedSolomonRemainder(block, spec.eccCodewordsPerBlock));
  const result: number[] = [];
  const maxDataLength = Math.max(...blocks.map((block) => block.length));

  for (let i = 0; i < maxDataLength; i += 1) {
    for (const block of blocks) {
      if (i < block.length) {
        result.push(block[i]);
      }
    }
  }
  for (let i = 0; i < spec.eccCodewordsPerBlock; i += 1) {
    for (const block of eccBlocks) {
      result.push(block[i]);
    }
  }
  return result;
}

function splitBlocks(data: number[], blockCount: number): number[][] {
  const blockLength = data.length / blockCount;
  const blocks: number[][] = [];
  for (let i = 0; i < blockCount; i += 1) {
    blocks.push(data.slice(i * blockLength, (i + 1) * blockLength));
  }
  return blocks;
}

function reedSolomonRemainder(data: number[], degree: number): number[] {
  const generator = reedSolomonGenerator(degree);
  const result = new Array<number>(degree).fill(0);

  for (const byte of data) {
    const factor = byte ^ result.shift()!;
    result.push(0);
    for (let i = 0; i < degree; i += 1) {
      result[i] ^= gfMultiply(generator[i], factor);
    }
  }
  return result;
}

function reedSolomonGenerator(degree: number): number[] {
  const cached = generatorCache.get(degree);
  if (cached) {
    return cached;
  }

  let result = [1];
  for (let i = 0; i < degree; i += 1) {
    const next = new Array<number>(result.length + 1).fill(0);
    for (let j = 0; j < result.length; j += 1) {
      next[j] ^= result[j];
      next[j + 1] ^= gfMultiply(result[j], gfExp[i]);
    }
    result = next;
  }

  const generator = result.slice(1);
  generatorCache.set(degree, generator);
  return generator;
}

function gfMultiply(left: number, right: number): number {
  if (left === 0 || right === 0) {
    return 0;
  }
  return gfExp[gfLog[left] + gfLog[right]];
}

function createBaseMatrix(spec: QrVersionSpec): { modules: boolean[][]; functionModules: boolean[][] } {
  const modules = Array.from({ length: spec.size }, () => new Array<boolean>(spec.size).fill(false));
  const functionModules = Array.from({ length: spec.size }, () => new Array<boolean>(spec.size).fill(false));

  drawFinderPattern(modules, functionModules, 3, 3);
  drawFinderPattern(modules, functionModules, spec.size - 4, 3);
  drawFinderPattern(modules, functionModules, 3, spec.size - 4);
  drawAlignmentPatterns(modules, functionModules, spec);
  drawTimingPatterns(modules, functionModules);
  reserveFormatBits(modules, functionModules, spec.size);
  setFunctionModule(modules, functionModules, 8, spec.size - 8, true);

  return { modules, functionModules };
}

function drawFinderPattern(modules: boolean[][], functionModules: boolean[][], centerX: number, centerY: number): void {
  for (let dy = -4; dy <= 4; dy += 1) {
    for (let dx = -4; dx <= 4; dx += 1) {
      const x = centerX + dx;
      const y = centerY + dy;
      if (!isInside(modules.length, x, y)) {
        continue;
      }
      const distance = Math.max(Math.abs(dx), Math.abs(dy));
      setFunctionModule(modules, functionModules, x, y, distance <= 3 && distance !== 2);
    }
  }
}

function drawAlignmentPatterns(modules: boolean[][], functionModules: boolean[][], spec: QrVersionSpec): void {
  if (spec.version === 1) {
    return;
  }

  const centers = [6, spec.size - 7];
  for (const y of centers) {
    for (const x of centers) {
      if (functionModules[y][x]) {
        continue;
      }
      drawAlignmentPattern(modules, functionModules, x, y);
    }
  }
}

function drawAlignmentPattern(modules: boolean[][], functionModules: boolean[][], centerX: number, centerY: number): void {
  for (let dy = -2; dy <= 2; dy += 1) {
    for (let dx = -2; dx <= 2; dx += 1) {
      const distance = Math.max(Math.abs(dx), Math.abs(dy));
      setFunctionModule(modules, functionModules, centerX + dx, centerY + dy, distance !== 1);
    }
  }
}

function drawTimingPatterns(modules: boolean[][], functionModules: boolean[][]): void {
  const size = modules.length;
  for (let i = 0; i < size; i += 1) {
    const dark = i % 2 === 0;
    if (!functionModules[6][i]) {
      setFunctionModule(modules, functionModules, i, 6, dark);
    }
    if (!functionModules[i][6]) {
      setFunctionModule(modules, functionModules, 6, i, dark);
    }
  }
}

function reserveFormatBits(modules: boolean[][], functionModules: boolean[][], size: number): void {
  for (let i = 0; i <= 5; i += 1) {
    setFunctionModule(modules, functionModules, 8, i, false);
    setFunctionModule(modules, functionModules, i, 8, false);
  }
  setFunctionModule(modules, functionModules, 8, 7, false);
  setFunctionModule(modules, functionModules, 8, 8, false);
  setFunctionModule(modules, functionModules, 7, 8, false);

  for (let i = 0; i < 8; i += 1) {
    setFunctionModule(modules, functionModules, size - 1 - i, 8, false);
  }
  for (let i = 0; i < 7; i += 1) {
    setFunctionModule(modules, functionModules, 8, size - 1 - i, false);
  }
}

function setFunctionModule(
  modules: boolean[][],
  functionModules: boolean[][],
  x: number,
  y: number,
  dark: boolean
): void {
  modules[y][x] = dark;
  functionModules[y][x] = true;
}

function isInside(size: number, x: number, y: number): boolean {
  return x >= 0 && y >= 0 && x < size && y < size;
}

function drawCodewords(modules: boolean[][], functionModules: boolean[][], codewords: number[]): void {
  const bits = codewords.flatMap((codeword) => byteToBits(codeword));
  let bitIndex = 0;
  let upward = true;

  for (let right = modules.length - 1; right > 0; right -= 2) {
    if (right === 6) {
      right -= 1;
    }
    for (let vertical = 0; vertical < modules.length; vertical += 1) {
      const y = upward ? modules.length - 1 - vertical : vertical;
      for (let dx = 0; dx < 2; dx += 1) {
        const x = right - dx;
        if (functionModules[y][x]) {
          continue;
        }
        modules[y][x] = bitIndex < bits.length ? bits[bitIndex] === 1 : false;
        bitIndex += 1;
      }
    }
    upward = !upward;
  }
}

function byteToBits(value: number): number[] {
  const bits: number[] = [];
  appendBits(bits, value, 8);
  return bits;
}

function applyMask(modules: boolean[][], functionModules: boolean[][]): void {
  for (let y = 0; y < modules.length; y += 1) {
    for (let x = 0; x < modules.length; x += 1) {
      if (!functionModules[y][x] && (x + y) % 2 === 0) {
        modules[y][x] = !modules[y][x];
      }
    }
  }
}

function drawFormatBits(modules: boolean[][], size: number, maskPattern: number): void {
  const bits = formatBits(maskPattern);
  for (let i = 0; i <= 5; i += 1) {
    modules[i][8] = getBit(bits, i);
  }
  modules[7][8] = getBit(bits, 6);
  modules[8][8] = getBit(bits, 7);
  modules[8][7] = getBit(bits, 8);
  for (let i = 9; i < 15; i += 1) {
    modules[8][14 - i] = getBit(bits, i);
  }

  for (let i = 0; i < 8; i += 1) {
    modules[8][size - 1 - i] = getBit(bits, i);
  }
  for (let i = 8; i < 15; i += 1) {
    modules[size - 15 + i][8] = getBit(bits, i);
  }
  modules[size - 8][8] = true;
}

function formatBits(maskPattern: number): number {
  const data = (FORMAT_ECC_LEVEL_L << 3) | maskPattern;
  let remainder = data << 10;
  for (let i = 14; i >= 10; i -= 1) {
    if (((remainder >>> i) & 1) !== 0) {
      remainder ^= 0x537 << (i - 10);
    }
  }
  return ((data << 10) | remainder) ^ 0x5412;
}

function getBit(value: number, index: number): boolean {
  return ((value >>> index) & 1) !== 0;
}

function matrixToPath(matrix: boolean[][], border: number): string {
  const segments: string[] = [];
  for (let y = 0; y < matrix.length; y += 1) {
    for (let x = 0; x < matrix.length; x += 1) {
      if (matrix[y][x]) {
        segments.push(`M${x + border},${y + border}h1v1h-1z`);
      }
    }
  }
  return segments.join('');
}

function escapeXml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}
