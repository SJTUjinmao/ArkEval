import { Project, SyntaxKind } from "ts-morph";

function toFnRef(node, kind, name) {
  return {
    kind,
    name: name ?? null,
    line_start: node.getStartLineNumber(),
    line_end: node.getEndLineNumber(),
  };
}

function getVariableFunctionInits(sourceFile) {
  const out = [];
  for (const decl of sourceFile.getVariableDeclarations()) {
    const init = decl.getInitializer();
    if (!init) continue;
    const k = init.getKind();
    if (k === SyntaxKind.ArrowFunction) {
      out.push(toFnRef(init, "ArrowFunction", decl.getName()));
    } else if (k === SyntaxKind.FunctionExpression) {
      out.push(toFnRef(init, "FunctionExpression", decl.getName()));
    }
  }
  return out;
}

function main() {
  const filePath = process.argv[2];
  if (!filePath) {
    console.error("Usage: node extract_functions.mjs <file.ts>");
    process.exit(2);
  }

  const project = new Project({
    useInMemoryFileSystem: false,
    skipAddingFilesFromTsConfig: true,
  });

  const sourceFile = project.addSourceFileAtPath(filePath);

  const functions = [];
  for (const fn of sourceFile.getFunctions()) {
    functions.push(toFnRef(fn, "FunctionDeclaration", fn.getName()));
  }

  for (const cls of sourceFile.getClasses()) {
    for (const ctor of cls.getConstructors()) {
      functions.push(toFnRef(ctor, "Constructor", `${cls.getName() ?? "<anonymous>"}.constructor`));
    }
    for (const m of cls.getMethods()) {
      functions.push(toFnRef(m, "MethodDeclaration", `${cls.getName() ?? "<anonymous>"}.${m.getName()}`));
    }
  }

  functions.push(...getVariableFunctionInits(sourceFile));

  // Sort and de-dup identical ranges
  functions.sort((a, b) => a.line_start - b.line_start || a.line_end - b.line_end);
  const dedup = [];
  const seen = new Set();
  for (const f of functions) {
    const key = `${f.line_start}:${f.line_end}:${f.kind}:${f.name ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    dedup.push(f);
  }

  process.stdout.write(JSON.stringify({ functions: dedup }));
}

main();
