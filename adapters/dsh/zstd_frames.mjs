// zstd_frames.mjs — DSH 会话日志解码器（核心：zstd 多帧）
//
// 为什么需要它：DSH 的 session.v4.jsonl.zstd 是「zstd 多帧」串联。
//   · zlib.zstdDecompressSync(buf)   → 只出第一帧   （实测 5,236,005 B 的文件只出 187 B）
//   · zlib.createZstdDecompress()    → 同样只出第一帧（实测同上，187 B）
//   · 两者都【不报错】—— 安静地丢掉 99.999%
//
// 怎么切帧：**按 zstd 帧结构解析**（帧头描述符 → 块头 → 块大小 → 校验和），
// 不是"找 4 字节魔数 28 B5 2F FD"。
//   为什么不用魔数扫描：如果这 4 个字节**出现在某个帧的压缩数据内部**，就会把一个帧劈成两半。
//   这不是理论风险 —— 实测：320 次里造出 40 次（`can_magic_false_positive.mjs`）。
//   本文件的结构解析是**移植 DSH 自己的实现**：
//     @deepseek-ai/dsh-session-persistence-jsonl/lib/index.js 的 scanZstdFrames()
//   （DSH 也是自己切帧再逐帧解 —— 它注明"Node 只把同步解码暴露成一发式 API"，
//     所以它去用 stream 的私有 handle 复用原生上下文。）
//
// 用法:
//   node zstd_frames.mjs <in.zstd> <out.jsonl>
// 退出码:
//   0 正常；3 输入不是合法的帧流；4 非末尾帧解压失败
// 副作用: 只写 <out.jsonl>（调用方负责清理）；绝不碰输入文件。
import fs from 'node:fs';
import zlib from 'node:zlib';

const ZSTD_MAGIC = 0xfd2fb528;          // 小端读出来等于字节 28 B5 2F FD
const MAGIC_BYTES = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);

function fail(code, msg) {
  process.stderr.write('zstd_frames: ' + msg + '\n');
  process.exit(code);
}

const argv = process.argv.slice(2);
if (argv.length !== 2) fail(3, 'usage: node zstd_frames.mjs <in.zstd> <out.jsonl>');
const [inp, outp] = argv;

let buf;
try {
  buf = fs.readFileSync(inp);
} catch (e) {
  fail(3, 'cannot read input: ' + e.message);
}

/**
 * 按结构定位完整帧（移植 DSH 的 scanZstdFrames）。
 * 返回 { frames:[{start,end}], tornStart }，tornStart 表示末尾有个没写完的帧。
 */
function scanFrames(buffer) {
  const frames = [];
  let checksumFrames = 0;
  let offset = 0;
  while (offset < buffer.length) {
    const start = offset;
    if (buffer.length - offset < 4) return { frames, tornStart: start, checksumFrames };
    if (buffer.readUInt32LE(offset) !== ZSTD_MAGIC) {
      fail(3, 'invalid frame magic at byte ' + offset + ' (not a zstd frame stream)');
    }
    offset += 4;
    if (offset === buffer.length) return { frames, tornStart: start, checksumFrames };
    const descriptor = buffer.readUInt8(offset);
    offset += 1;
    if ((descriptor & 24) !== 0) fail(4, 'reserved frame-header bit at byte ' + (offset - 1));
    const contentSizeFlag = descriptor >>> 6;
    const singleSegment = (descriptor & 32) !== 0;
    const checksum = (descriptor & 4) !== 0;
    const dictionaryFlag = descriptor & 3;
    const dictionaryBytes = dictionaryFlag === 3 ? 4 : dictionaryFlag;
    const contentSizeBytes = contentSizeFlag === 0 ? (singleSegment ? 1 : 0) : 1 << contentSizeFlag;
    const remainingHeaderBytes = (singleSegment ? 0 : 1) + dictionaryBytes + contentSizeBytes;
    if (buffer.length - offset < remainingHeaderBytes) return { frames, tornStart: start, checksumFrames };
    offset += remainingHeaderBytes;
    for (;;) {
      if (buffer.length - offset < 3) return { frames, tornStart: start, checksumFrames };
      const blockHeader = buffer.readUIntLE(offset, 3);
      offset += 3;
      const lastBlock = (blockHeader & 1) !== 0;
      const blockType = (blockHeader >>> 1) & 3;
      const blockSize = blockHeader >>> 3;
      if (blockType === 3) fail(4, 'reserved block type at byte ' + (offset - 3));
      const payloadBytes = blockType === 1 ? 1 : blockSize;
      if (buffer.length - offset < payloadBytes) return { frames, tornStart: start, checksumFrames };
      offset += payloadBytes;
      if (lastBlock) break;
    }
    if (checksum) {
      if (buffer.length - offset < 4) return { frames, tornStart: start, checksumFrames };
      offset += 4;
      checksumFrames++;
    }
    frames.push({ start, end: offset });
  }
  return { frames, checksumFrames };
}

if (buf.length === 0) fail(3, 'input is empty; not a zstd frame stream');
if (buf.length < 4) fail(3, 'input shorter than a zstd frame header (' + buf.length + ' B)');
if (buf.readUInt32LE(0) !== ZSTD_MAGIC) {
  fail(3, 'no zstd frame at offset 0; not a zstd frame stream');
}

const { frames, tornStart, checksumFrames } = scanFrames(buf);

// 逐帧解压（每帧独立可解；带校验和的帧由 zlib 校验）
const parts = [];
for (let k = 0; k < frames.length; k++) {
  const { start, end } = frames[k];
  let out;
  try {
    out = zlib.zstdDecompressSync(buf.subarray(start, end));
  } catch (err) {
    fail(4, 'frame at byte ' + start + ' failed to decompress: ' + err.message);
  }
  parts.push(out);
}

// 二次防线：帧边界应当就是行边界。末帧不以换行结尾 ⇒ 半条，丢掉。
let truncatedTail = tornStart !== undefined ? 1 : 0;
const last = parts[parts.length - 1];
if (last && !last.toString('latin1').endsWith('\n')) {
  truncatedTail = 1;
  parts.pop();
}

const combined = Buffer.concat(parts);
const text = combined.toString('utf8');
const lineCount = text.length ? text.split('\n').filter((x) => x.length > 0).length : 0;
fs.writeFileSync(outp, combined);

// 读数打到 stdout（给调用方做判据，不打印任何正文）
process.stdout.write(JSON.stringify({
  frames: frames.length + (tornStart !== undefined ? 1 : 0),
  frames_decoded: parts.length,
  structurally_complete_frames: frames.length,
  torn_tail_frame_start: tornStart === undefined ? null : tornStart,
  checksum_frames: checksumFrames,
  truncated_tail_frame_dropped: truncatedTail === 1,
  bytes_in: buf.length,
  bytes_out: combined.length,
  lines: lineCount,
  sha256_out: (await import('node:crypto')).createHash('sha256').update(combined).digest('hex')
}) + '\n');
