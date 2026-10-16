// zstd_pack.mjs — 把一个 JSONL 打包成【zstd 多帧】（做测试夹具用）。
//
// 它存在的理由：DSH 的 session.v4.jsonl.zstd 是多帧；要测多帧解码器，
// 就得能造出多帧样本 —— 而且必须是合成的，不能用任何真实会话。
//
// 用法: node zstd_pack.mjs <in.jsonl> <out.zstd> [每帧行数]
// 退出码: 0 正常；3 参数/输入有问题
import fs from 'node:fs';
import zlib from 'node:zlib';

const argv = process.argv.slice(2);
if (argv.length < 2 || argv.length > 3) {
  process.stderr.write('usage: node zstd_pack.mjs <in.jsonl> <out.zstd> [linesPerFrame]\n');
  process.exit(3);
}
const [inp, outp, perArg] = argv;
const per = Math.max(1, parseInt(perArg || '1', 10) || 1);

let text;
try {
  text = fs.readFileSync(inp, 'utf8');
} catch (e) {
  process.stderr.write('zstd_pack: cannot read input: ' + e.message + '\n');
  process.exit(3);
}

const lines = text.split('\n').filter((x) => x.length > 0);
if (!lines.length) {
  process.stderr.write('zstd_pack: input has no non-empty lines\n');
  process.exit(3);
}

const frames = [];
for (let i = 0; i < lines.length; i += per) {
  const chunk = lines.slice(i, i + per).map((l) => l + '\n').join('');
  frames.push(zlib.zstdCompressSync(Buffer.from(chunk, 'utf8')));
}
fs.writeFileSync(outp, Buffer.concat(frames));
process.stdout.write(JSON.stringify({
  frames: frames.length, lines: lines.length,
  lines_per_frame: per, bytes_out: fs.statSync(outp).size
}) + '\n');
