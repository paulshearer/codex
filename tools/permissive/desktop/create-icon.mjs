// A small terminal mark; no external asset or network request is required.
import { mkdirSync, writeFileSync } from 'node:fs';
import { deflateSync } from 'node:zlib';
import path from 'node:path';
const size = 64;
const pixels = Buffer.alloc(size * (1 + size * 4));
for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
  const foreground = ((x >= 17 && x <= 29 && Math.abs(y - 29) === x - 17) || (x >= 34 && x <= 48 && y >= 40 && y <= 43));
  const offset = y * (size * 4 + 1) + 1 + x * 4;
  pixels.set(foreground ? [94, 234, 212, 255] : [17, 24, 39, 255], offset);
}
function crc32(bytes) { let result = 0xffffffff; for (const byte of bytes) { result ^= byte; for (let bit = 0; bit < 8; bit++) result = (result >>> 1) ^ ((result & 1) ? 0xedb88320 : 0); } return (result ^ 0xffffffff) >>> 0; }
function chunk(type, bytes) { const body = Buffer.concat([Buffer.from(type), bytes]); const length = Buffer.alloc(4), checksum = Buffer.alloc(4); length.writeUInt32BE(bytes.length); checksum.writeUInt32BE(crc32(body)); return Buffer.concat([length, body, checksum]); }
const header = Buffer.alloc(13); header.writeUInt32BE(size); header.writeUInt32BE(size, 4); header[8] = 8; header[9] = 6;
const png = Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', header), chunk('IDAT', deflateSync(pixels)), chunk('IEND', Buffer.alloc(0))]);
const ico = Buffer.alloc(22); ico.writeUInt16LE(1, 2); ico.writeUInt16LE(1, 4); ico[6] = size; ico[7] = size; ico.writeUInt16LE(1, 10); ico.writeUInt16LE(32, 12); ico.writeUInt32LE(png.length, 14); ico.writeUInt32LE(22, 18);
const directory = path.join(import.meta.dirname, '../desktop-rs/icons'); mkdirSync(directory, { recursive: true });
writeFileSync(path.join(directory, 'icon.png'), png); writeFileSync(path.join(directory, 'icon.ico'), Buffer.concat([ico, png]));
