import { deflateSync } from 'node:zlib'
import { writeFileSync } from 'node:fs'

function crc(data) {
  let value = 0xffffffff
  for (const byte of data) {
    value ^= byte
    for (let bit = 0; bit < 8; bit++) value = (value >>> 1) ^ (0xedb88320 & -(value & 1))
  }
  return (value ^ 0xffffffff) >>> 0
}

function chunk(name, data) {
  const type = Buffer.from(name)
  const length = Buffer.alloc(4); length.writeUInt32BE(data.length)
  const checksum = Buffer.alloc(4); checksum.writeUInt32BE(crc(Buffer.concat([type, data])))
  return Buffer.concat([length, type, data, checksum])
}

function icon(size) {
  const raw = Buffer.alloc((size * 4 + 1) * size)
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    const offset = y * (size * 4 + 1) + 1 + x * 4
    const whiteC = x > size * .27 && x < size * .72 && y > size * .27 && y < size * .73 && (x < size * .39 || y < size * .39 || y > size * .61)
    const greenDot = (x - size * .67) ** 2 + (y - size * .56) ** 2 < (size * .065) ** 2
    const color = greenDot ? [54, 160, 120] : whiteC ? [255, 255, 255] : [23, 23, 23]
    raw.set([...color, 255], offset)
  }
  const header = Buffer.alloc(13)
  header.writeUInt32BE(size, 0); header.writeUInt32BE(size, 4)
  header.set([8, 6, 0, 0, 0], 8)
  return Buffer.concat([Buffer.from('\x89PNG\r\n\x1a\n', 'binary'), chunk('IHDR', header), chunk('IDAT', deflateSync(raw)), chunk('IEND', Buffer.alloc(0))])
}

for (const size of [192, 512]) writeFileSync(new URL(`../public/icon-${size}.png`, import.meta.url), icon(size))
