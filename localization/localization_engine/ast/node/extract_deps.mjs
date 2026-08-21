/**
 * 输出文件的依赖相关信息：相对 import、导出符号、类型引用（用于类型依赖追踪）。
 * 用法: node extract_deps.mjs <file.ts>
 * 输出 JSON: { imports: string[], exports: string[], typeRefs: string[] }
 */
import { Project, SyntaxKind } from "ts-morph";
import { readFileSync } from "node:fs";

const BUILTIN_TYPES = new Set([
  "string", "number", "boolean", "void", "any", "unknown", "never", "undefined", "null",
  "Object", "Array", "Promise", "Map", "Set", "WeakMap", "WeakSet", "Date", "RegExp",
  "Error", "Function", "Symbol", "BigInt", "Intl", "JSON", "Math", "Console",
  "HTMLElement", "Element", "Node", "Document", "Window", "Event", "Response", "Request",
  "Record", "Partial", "Required", "Readonly", "Pick", "Omit", "Exclude", "Extract",
  "ReturnType", "Parameters", "NonNullable", "InstanceType", "ThisType",
]);

function getRootTypeName(node) {
  const nameNode = node.getTypeName();
  if (!nameNode) return null;
  const text = nameNode.getText();
  if (!text) return null;
  const root = text.split(".")[0].trim();
  return root || null;
}

function extractFile(project, filePath) {
  let sourceFile;
  try {
    sourceFile = project.addSourceFileAtPath(filePath);
    const imports = new Set();
    for (const imp of sourceFile.getImportDeclarations()) {
      const spec = imp.getModuleSpecifierValue();
      if (typeof spec === "string" && (spec.startsWith("./") || spec.startsWith(".."))) {
        imports.add(spec);
      }
    }

    const exportsSet = new Set();
    const exportedDeclarations = sourceFile.getExportedDeclarations();
    for (const [name] of exportedDeclarations) {
      exportsSet.add(name);
    }
    for (const exp of sourceFile.getExportDeclarations()) {
      const named = exp.getNamedExports();
      for (const n of named) {
        const name = n.getNameNode().getText();
        if (name) exportsSet.add(name);
      }
    }

    const typeRefs = new Set();
    for (const node of sourceFile.getDescendants()) {
      if (node.getKind() !== SyntaxKind.TypeReference) continue;
      const root = getRootTypeName(node);
      if (root && !BUILTIN_TYPES.has(root)) {
        typeRefs.add(root);
      }
    }

    return {
      ok: true,
      imports: Array.from(imports),
      exports: Array.from(exportsSet),
      typeRefs: Array.from(typeRefs),
    };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    };
  } finally {
    for (const addedSource of project.getSourceFiles()) {
      project.removeSourceFile(addedSource);
    }
  }
}

function main() {
  let filePaths = process.argv.slice(2);
  if (filePaths.length === 1 && filePaths[0] === "--stdin") {
    try {
      filePaths = JSON.parse(readFileSync(0, "utf8"));
    } catch (e) {
      console.error("Invalid JSON file list on stdin");
      process.exit(2);
    }
  }
  if (filePaths.length === 0) {
    console.error("Usage: node extract_deps.mjs <file.ts> [more files...]");
    process.exit(2);
  }

  const project = new Project({
    useInMemoryFileSystem: false,
    skipAddingFilesFromTsConfig: true,
  });

  if (filePaths.length === 1) {
    process.stdout.write(JSON.stringify(extractFile(project, filePaths[0])));
    return;
  }

  const results = filePaths.map((filePath) => ({
    path: filePath,
    ...extractFile(project, filePath),
  }));
  process.stdout.write(JSON.stringify({ results }));
}

main();
