/**
 * ArkTS .ets experimental extractor.
 *
 * 目标：在不依赖现有 indexer 的前提下，为 .ets 文件产出“近似 AST 的行号范围”，
 * 供 Python 侧实验分块使用。
 *
 * 设计为两层：
 * 1. 若环境变量 ARKTS_AST_CMD 已配置，优先调用外部 ArkTS AST 工具：
 *    - 例如：ARKTS_AST_CMD="arktsc --dump-ast"
 *    - 本脚本作为适配层，只需把 filePath 传给该命令，并从其 JSON AST 中提取 ranges。
 *    - 这里留出接口，但默认实现暂不假设外部工具存在。
 * 2. 若未配置 ARKTS_AST_CMD 或外部工具失败，则使用本地启发式解析：
 *    - 基于正则 + 大括号计数，识别 struct / @Entry struct / build() 等结构，
 *      输出若干 { line_start, line_end, kind, name } 范围。
 *
 * 用法：
 *   node arkts-extractor.mjs --file /abs/path/to/file.ets [--root /abs/repo/root]
 *
 * 输出：
 *   { "ranges": [ { "line_start": 10, "line_end": 40, "kind": "struct", "name": "Index" }, ... ] }
 *   失败时：
 *   { "ranges": [], "error": "message" }
 */

import fs from "fs";
import path from "path";
import { spawnSync } from "child_process";

function parseArgs() {
  const args = process.argv.slice(2);
  const out = { file: null, root: null };
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "--file" && i + 1 < args.length) {
      out.file = args[++i];
    } else if (a === "--root" && i + 1 < args.length) {
      out.root = args[++i];
    }
  }
  return out;
}

function readLines(filePath) {
  try {
    const text = fs.readFileSync(filePath, { encoding: "utf-8" });
    // 保留行号信息
    return text.split(/\r?\n/);
  } catch (e) {
    return null;
  }
}

/**
 * 简易 ArkTS 结构识别：
 * - struct Xxx / @Entry ... struct Xxx：只输出 struct 头几行（到第一个方法前），不输出整块到文件尾，避免按字符切分时在 build() 中间截断。
 * - aboutToAppear() { / aboutToDisappear() {：生命周期方法单独成块。
 * - build() { ... }：单独成块。
 *
 * 通过大括号计数找到块的结束行（仅在该方法体内计数，不把整 struct 当一块）。
 */
function heuristicArktsRanges(lines) {
  const ranges = [];
  const structStartRegex = /^\s*(?:@Entry\s+)?export\s+struct\s+(\w+)/;
  const plainStructRegex = /^\s*struct\s+(\w+)/;
  const buildRegex = /^\s*(\w+)?\s*build\s*\(\s*\)\s*{/;
  const lifecycleRegex = /^\s*(aboutToAppear|aboutToDisappear)\s*\(\s*\)\s*{/;

  const n = lines.length;

  function findBlockEnd(startLineIndex) {
    let depth = 0;
    let started = false;
    for (let i = startLineIndex; i < n; i++) {
      const line = lines[i];
      for (const ch of line) {
        if (ch === "{") {
          depth += 1;
          started = true;
        } else if (ch === "}") {
          depth -= 1;
        }
      }
      if (started && depth <= 0) {
        return i + 1;
      }
    }
    return startLineIndex + 1;
  }

  // 第一遍：收集第一个方法起始行，用于 struct 只输出“头”
  const methodStartLines = [];
  for (let i = 0; i < n; i++) {
    const line = lines[i];
    if (lifecycleRegex.test(line) || buildRegex.test(line)) {
      methodStartLines.push(i + 1);
    }
  }
  const firstMethodLine = methodStartLines.length > 0 ? Math.min(...methodStartLines) : null;

  for (let i = 0; i < n; i++) {
    const line = lines[i];

    const life = lifecycleRegex.exec(line);
    if (life) {
      const methodName = life[1];
      const endLine = findBlockEnd(i);
      ranges.push({
        line_start: i + 1,
        line_end: endLine,
        kind: "lifecycle",
        name: methodName,
      });
      continue;
    }

    const b = buildRegex.exec(line);
    if (b) {
      const endLine = findBlockEnd(i);
      ranges.push({
        line_start: i + 1,
        line_end: endLine,
        kind: "build",
        name: "build",
      });
      continue;
    }

    let m = structStartRegex.exec(line);
    if (!m) {
      m = plainStructRegex.exec(line);
    }
    if (m) {
      const name = m[1] || "struct";
      // 只输出 struct 头：到第一个方法前一行，或 struct 行后一两行，避免整块到文件尾
      const headerEnd = firstMethodLine != null ? firstMethodLine - 1 : Math.min(i + 2, n);
      ranges.push({
        line_start: i + 1,
        line_end: headerEnd,
        kind: "struct_header",
        name,
      });
      continue;
    }
  }

  ranges.sort((a, b) => a.line_start - b.line_start);
  return ranges;
}

async function main() {
  const { file, root } = parseArgs();
  if (!file) {
    process.stderr.write("Usage: node arkts-extractor.mjs --file <file.ets> [--root <repo_root>]\\n");
    process.exit(2);
  }

  const absFile = path.resolve(file);
  const lines = readLines(absFile);
  if (!lines) {
    process.stdout.write(JSON.stringify({ ranges: [], error: "failed to read file" }));
    process.exit(1);
  }

  // 优先使用 OpenHarmony 官方 ArkTS 语法树（ets2panda --dump-ast）
  const ets2pandaPath = process.env.ARKTS_ETS2PANDA;
  const astCmd = process.env.ARKTS_AST_CMD;
  const rangesFromOfficial = tryOfficialArktsAst(ets2pandaPath || astCmd, absFile, root || "");

  if (rangesFromOfficial && rangesFromOfficial.length > 0) {
    const semantic = toSemanticNonOverlappingRanges(rangesFromOfficial, lines.length);
    const finalized = finalizeRanges(semantic, lines.length);
    process.stdout.write(JSON.stringify({ ranges: finalized, source: "official_ast" }));
    process.exit(0);
  }

  const ranges = heuristicArktsRanges(lines);
  const finalized = finalizeRanges(ranges, lines.length);
  process.stdout.write(JSON.stringify({ ranges: finalized, source: "heuristic" }));
  process.exit(0);
}

/**
 * 调用官方 ets2panda（或 ARKTS_AST_CMD）获取 AST，并解析为 ranges。
 * ARKTS_ETS2PANDA：可执行文件路径，脚本会追加 --dump-ast 与文件路径。
 * ARKTS_AST_CMD：完整命令模板，可用 {file}、{root} 占位符。
 */
function tryOfficialArktsAst(cmdOrPath, absFile, root) {
  if (!cmdOrPath || !absFile) return null;

  let cmdArgs;
  if (cmdOrPath.includes(" ") || cmdOrPath.includes("{file}")) {
    const str = cmdOrPath.replace(/\{file\}/g, absFile).replace(/\{root\}/g, root).trim();
    cmdArgs = str.split(/\s+/);
  } else {
    cmdArgs = [cmdOrPath, absFile, "--dump-ast"];
  }
  const exe = cmdArgs[0];
  const args = cmdArgs.slice(1);

  try {
    const r = spawnSync(exe, args, {
      encoding: "utf-8",
      timeout: 15000,
      maxBuffer: 4 * 1024 * 1024,
    });
    const out = (r.stdout || "").trim();
    if (r.status !== 0 && !out) return null;

    const parsed = parseOfficialAstOutput(out);
    if (parsed && parsed.length > 0) return parsed;
  } catch (_) {
    return null;
  }
  return null;
}

/**
 * 解析官方 AST 输出。支持：
 * - JSON：节点含 lineStart/lineEnd 或 range、loc 等行号信息。
 * - 文本树：每行形如 "NodeType [line:col-line:col]" 等，用正则提取行号。
 */
function parseOfficialAstOutput(raw) {
  const ranges = [];
  const lineRe = /^\s*(\w+(?:Declaration|Statement|Expression)?)\s*\[?\s*(\d+)\s*[:\-]\s*(\d+)\s*[:\-]\s*(\d+)\s*[:\-]\s*(\d+)\s*\]?/;
  const lineRe2 = /^\s*(\w+)\s+(\d+)\s*:\s*(\d+)\s*-\s*(\d+)\s*:\s*(\d+)/;

  try {
    const j = JSON.parse(raw);
    const visit = (n) => {
      if (!n || typeof n !== "object") return;
      const r = jsonNodeToRange(n);
      if (r) ranges.push(r);
      for (const key of Object.keys(n)) {
        const v = n[key];
        if (Array.isArray(v)) v.forEach(visit);
        else if (v && typeof v === "object" && (v.loc != null || v.type != null)) visit(v);
      }
    };
    if (Array.isArray(j)) j.forEach(visit);
    else if (j && typeof j === "object") visit(j);
    if (ranges.length > 0) return ranges;
  } catch (_) {}

  for (const line of raw.split("\n")) {
    const m = lineRe.exec(line) || lineRe2.exec(line);
    if (m) {
      const lineStart = parseInt(m[2], 10);
      const lineEnd = parseInt(m[4], 10);
      if (lineStart >= 1 && lineEnd >= lineStart) {
        ranges.push({
          line_start: lineStart,
          line_end: lineEnd,
          kind: (m[1] || "node").toLowerCase(),
          name: m[1] || "node",
        });
      }
    }
  }
  return ranges.length > 0 ? ranges : null;
}

function jsonNodeToRange(node) {
  if (!node || typeof node !== "object") return null;
  let lineStart = node.lineStart ?? node.startLine ?? node.line ?? node.range?.[0]?.[0];
  let lineEnd = node.lineEnd ?? node.endLine ?? node.line ?? node.range?.[1]?.[0];
  if (node.range && Array.isArray(node.range) && node.range[0] != null) {
    lineStart = node.range[0].line ?? node.range[0][0];
    lineEnd = node.range[1].line ?? node.range[1][0];
  }
  if (lineStart == null && node.loc) {
    lineStart = node.loc.start?.line ?? node.loc.startLine;
    lineEnd = node.loc.end?.line ?? node.loc.endLine;
  }
  if (lineStart == null || lineEnd == null) return null;
  lineStart = parseInt(lineStart, 10);
  lineEnd = parseInt(lineEnd, 10);
  if (lineStart < 1 || lineEnd < lineStart) return null;
  const kind = node.type ?? node.kind ?? node.nodeType ?? "node";
  const name = node.name?.name ?? node.name ?? node.id?.name ?? kind;
  return { line_start: lineStart, line_end: lineEnd, kind: String(kind), name: String(name) };
}

function toSemanticNonOverlappingRanges(ranges, totalLines) {
  if (!Array.isArray(ranges) || ranges.length === 0) return [];

  const semanticKinds = new Set([
    "ImportDeclaration",
    "ExportNamedDeclaration",
    "ExportDefaultDeclaration",
    "ClassDeclaration",
    "StructDeclaration",
    "MethodDefinition",
    "FunctionDeclaration",
    "TSInterfaceDeclaration",
    "TSEnumDeclaration",
    "TSTypeAliasDeclaration",
    "VariableDeclaration",
  ]);

  const priority = {
    MethodDefinition: 1,
    FunctionDeclaration: 1,
    ClassDeclaration: 2,
    StructDeclaration: 2,
    TSInterfaceDeclaration: 3,
    TSEnumDeclaration: 3,
    TSTypeAliasDeclaration: 3,
    ExportNamedDeclaration: 4,
    ExportDefaultDeclaration: 4,
    VariableDeclaration: 5,
    ImportDeclaration: 6,
    class_header: 7,
  };

  const dedup = [];
  const seen = new Set();
  for (const r of ranges) {
    if (!r || r.line_start == null || r.line_end == null || !r.kind) continue;
    const key = `${r.kind}|${r.line_start}|${r.line_end}|${r.name || ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    dedup.push(r);
  }

  const filtered = dedup.filter((r) => {
    const kind = String(r.kind);
    if (!semanticKinds.has(kind)) return false;
    const span = r.line_end - r.line_start + 1;
    if (kind === "Program") return false;
    if (totalLines > 0 && span >= totalLines) return false;
    if (kind.startsWith("Export") && totalLines > 0 && span > Math.floor(totalLines * 0.8)) return false;
    return span >= 1;
  });

  const methodRanges = filtered.filter((r) => r.kind === "MethodDefinition");
  const classLike = filtered.filter((r) => r.kind === "ClassDeclaration" || r.kind === "StructDeclaration");

  const syntheticHeaders = [];
  for (const c of classLike) {
    const methodsInClass = methodRanges
      .filter((m) => m.line_start > c.line_start && m.line_end <= c.line_end)
      .sort((a, b) => a.line_start - b.line_start);
    const firstMethod = methodsInClass.length > 0 ? methodsInClass[0] : null;
    const headerEnd = firstMethod ? firstMethod.line_start - 1 : c.line_end;
    if (headerEnd >= c.line_start) {
      syntheticHeaders.push({
        line_start: c.line_start,
        line_end: headerEnd,
        kind: "class_header",
        name: `${c.name || "class"}_header`,
      });
    }
  }

  const rangesForSelect = filtered.filter((r) => r.kind !== "ClassDeclaration" && r.kind !== "StructDeclaration");
  rangesForSelect.push(...syntheticHeaders);

  rangesForSelect.sort((a, b) => {
    const pA = priority[a.kind] ?? 99;
    const pB = priority[b.kind] ?? 99;
    if (pA !== pB) return pA - pB;
    const spanA = a.line_end - a.line_start;
    const spanB = b.line_end - b.line_start;
    const preferSmall = a.kind === "MethodDefinition" || a.kind === "FunctionDeclaration";
    if (spanA !== spanB) return preferSmall ? spanA - spanB : spanB - spanA;
    if (a.line_start !== b.line_start) return a.line_start - b.line_start;
    return String(a.kind).localeCompare(String(b.kind));
  });

  const selected = [];
  for (const r of rangesForSelect) {
    const overlap = selected.some((s) => !(r.line_end < s.line_start || r.line_start > s.line_end));
    if (!overlap) selected.push(r);
  }

  selected.sort((a, b) => a.line_start - b.line_start || a.line_end - b.line_end);
  return selected;
}

function finalizeRanges(ranges, totalLines) {
  if (!Array.isArray(ranges)) return [];
  const sorted = [...ranges].sort((a, b) => a.line_start - b.line_start || a.line_end - b.line_end);
  const mergedImports = mergeConsecutiveImports(sorted);
  const withCoverage = fillGapsWithMisc(mergedImports, totalLines);
  return withCoverage;
}

function mergeConsecutiveImports(ranges) {
  const out = [];
  let i = 0;
  while (i < ranges.length) {
    const cur = ranges[i];
    if (cur.kind === "ImportDeclaration") {
      let start = cur.line_start;
      let end = cur.line_end;
      let j = i + 1;
      while (j < ranges.length) {
        const next = ranges[j];
        if (next.kind !== "ImportDeclaration") break;
        if (next.line_start <= end + 1) {
          end = Math.max(end, next.line_end);
          j += 1;
          continue;
        }
        break;
      }
      out.push({ line_start: start, line_end: end, kind: "imports", name: "imports" });
      i = j;
      continue;
    }
    out.push(cur);
    i += 1;
  }
  return out;
}

function fillGapsWithMisc(ranges, totalLines) {
  const out = [];
  let cursor = 1;
  for (const r of ranges) {
    if (!r || r.line_start == null || r.line_end == null) continue;
    if (r.line_start > cursor) {
      out.push({ line_start: cursor, line_end: r.line_start - 1, kind: "misc", name: "misc" });
    }
    if (r.line_end >= r.line_start) {
      out.push(r);
      cursor = r.line_end + 1;
    }
  }
  if (totalLines >= cursor) {
    out.push({ line_start: cursor, line_end: totalLines, kind: "misc", name: "misc" });
  }
  return out;
}

main().catch((e) => {
  process.stdout.write(JSON.stringify({ ranges: [], error: String(e && e.message ? e.message : e) }));
  process.exit(1);
});

