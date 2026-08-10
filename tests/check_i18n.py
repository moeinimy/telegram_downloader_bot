"""Every literal passed to t() must have an English entry in utils/i18n.py."""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The translation map, read statically so this does not need the bot's deps.
i18n_tree = ast.parse((ROOT / "utils" / "i18n.py").read_text(encoding="utf-8"))
en = None
for node in ast.walk(i18n_tree):
    if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_EN":
        en = ast.literal_eval(node.value)
if en is None:
    sys.exit("could not find _EN in utils/i18n.py")

missing, dynamic = [], []
for path in sorted(ROOT.glob("handlers/*.py")) + sorted(ROOT.glob("modules/*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "t"):
            continue
        if len(node.args) < 2:
            continue
        arg = node.args[1]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            if arg.value not in en:
                missing.append((path.name, node.lineno, arg.value))
        else:
            dynamic.append((path.name, node.lineno))

print(f"translation map: {len(en)} entries")
if dynamic:
    print(f"\n{len(dynamic)} non-literal t() call(s) - cannot be checked statically:")
    for name, line in dynamic:
        print(f"  {name}:{line}")
if missing:
    print(f"\n{len(missing)} MISSING English translation(s):")
    for name, line, text in missing:
        print(f"  {name}:{line}  {text[:70]!r}")
    sys.exit(1)
print("\nOK - every t() literal has an English entry")
