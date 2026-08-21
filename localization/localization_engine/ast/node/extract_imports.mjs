/**
 * 输出文件中所有相对路径的 import 的 module specifier（不解析为绝对路径，由 Python 解析）。
 * 仅输出以 . 或 ./ 开头的相对导入。
 */
import { Project } from "ts-morph";

function main() {
  const filePath = process.argv[2];
  if (!filePath) {
    console.error("Usage: node extract_imports.mjs <file.ts>");
    process.exit(2);
  }

  const project = new Project({
    useInMemoryFileSystem: false,
    skipAddingFilesFromTsConfig: true,
  });

  let sourceFile;
  try {
    sourceFile = project.addSourceFileAtPath(filePath);
  } catch (e) {
    process.stdout.write(JSON.stringify({ imports: [] }));
    return;
  }

  const specifiers = new Set();
  for (const imp of sourceFile.getImportDeclarations()) {
    const spec = imp.getModuleSpecifierValue();
    if (typeof spec === "string" && (spec.startsWith("./") || spec.startsWith(".."))) {
      specifiers.add(spec);
    }
  }
  // type-only / export from 等也可能在 getImportDeclarations 里
  process.stdout.write(JSON.stringify({ imports: Array.from(specifiers) }));
}

main();
