"""Stage the SalesAgent docker deploy bundle (default C:\\Apps\\salesagent):
consistent DB snapshots (safe while the server runs), uploads/files/estate,
container-path-fixed trends pointer, .env, prompts dir.

Usage:  python scripts/make_bundle.py [bundle_dir]
The bundle is what the container mounts — it carries ALL state (users, chat
history, artifacts, caches) plus the .env. Move it between machines by file
copy; it never goes to git.
"""
import json
import shutil
import sqlite3
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
CANDIDATES = ([Path(sys.argv[1])] if len(sys.argv) > 1 else
              [Path(r"C:\Apps\salesagent"), SRC / "deploy" / "bundle"])

bundle = None
for cand in CANDIDATES:
    try:
        (cand / "data").mkdir(parents=True, exist_ok=True)
        (cand / "prompts").mkdir(parents=True, exist_ok=True)
        bundle = cand
        break
    except PermissionError:
        continue
if bundle is None:
    sys.exit("no writable bundle location")
print("bundle:", bundle)

# consistent snapshots via the sqlite backup API (fine on a live WAL db)
for name in ("app.db", "checkpoints.db"):
    src_db = SRC / "data" / name
    if not src_db.exists():
        print("  skip (missing):", name)
        continue
    dst = bundle / "data" / name
    for junk in (dst, dst.with_suffix(dst.suffix + "-wal"),
                 dst.with_suffix(dst.suffix + "-shm")):
        junk.unlink(missing_ok=True)
    s = sqlite3.connect(str(src_db))
    d = sqlite3.connect(str(dst))
    with d:
        s.backup(d)
    s.close(); d.close()
    print(f"  db snapshot: {name} ({dst.stat().st_size:,} bytes)")

for sub in ("uploads", "files", "estate"):
    src_t = SRC / "data" / sub
    if src_t.exists():
        dst_t = bundle / "data" / sub
        if dst_t.exists():
            shutil.rmtree(dst_t)
        shutil.copytree(src_t, dst_t)
        n = sum(1 for f in dst_t.rglob("*") if f.is_file())
        print(f"  tree: {sub}/ ({n} files)")

# trends pointer: absolute host path -> container path
ptr_src = SRC / "data" / "trends_source.json"
if ptr_src.exists():
    ptr = json.loads(ptr_src.read_text(encoding="utf-8"))
    fname = Path(ptr["path"]).name
    ptr["path"] = f"C:/data/uploads/{fname}"
    (bundle / "data" / "trends_source.json").write_text(
        json.dumps(ptr), encoding="utf-8")
    print("  trends pointer ->", ptr["path"])

if (SRC / ".env").exists():
    shutil.copy2(SRC / ".env", bundle / ".env")
shutil.copy2(SRC / "salesagent" / "agent" / "prompts" / "system.md",
             bundle / "prompts" / "system.md")
print("  .env + prompts/system.md copied")

conn = sqlite3.connect(str(bundle / "data" / "app.db"))
conn.row_factory = sqlite3.Row
users = [r["username"] for r in conn.execute("SELECT username FROM users")]
msgs = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
convos = conn.execute("SELECT COUNT(*) c FROM conversations").fetchone()["c"]
conn.close()
print(f"  snapshot: users={users}, conversations={convos}, messages={msgs}")
print("BUNDLE READY")
