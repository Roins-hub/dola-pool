"""把 ck 目录批量导入号池。

目录结构（用户从卖家拿到的一批号）：
    <根目录>/162/cookie_39doula1a099.txt   # 每个 .txt = 一个账号，内容是一行原始 cookie 串
    <根目录>/163/...
    <根目录>/164/...

账号名 = 文件名去 .txt，再去掉开头的 "cookie_" 前缀（cookie_39doula1a099 -> 39doula1a099）。
为每个账号创建 accounts/<name>/cookies.json，浏览器启动时由
browser._inject_saved_cookies 注入（格式见 import_cookies.py）。

用法:
    python import_ck_dir.py <根目录> [--force] [--note-dir] [--dry-run]

默认跳过已存在的账号（不覆盖 cookies.json），--force 覆盖。
--note-dir 会把 accounts_meta.note 写为 ck/<子目录名>（仅当该号 note 为空）。
"""
import argparse
import json
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PREFIX = "cookie_"


def parse_cookie_string(raw: str) -> list[tuple[str, str]]:
    pairs = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key, value = key.strip(), value.strip()
        if key:
            pairs.append((key, value))
    return pairs


def account_name(stem: str) -> str:
    if stem.startswith(PREFIX) and len(stem) > len(PREFIX):
        return stem[len(PREFIX):]
    return stem


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--force", action="store_true", help="已存在的账号也覆盖 cookies.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--note-dir", action="store_true",
                    help="给新号写 note=ck/<子目录名>（需要 pool_usage.db 可写）")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"[error] 不是目录: {root}")
        return 2

    files = sorted(p for p in root.rglob("*.txt") if p.is_file())
    if not files:
        print(f"[error] {root} 下没有 .txt")
        return 2

    imported, skipped, failed = 0, 0, 0
    seen_names: dict[str, str] = {}
    seen_md5: dict[str, str] = {}
    notes: list[tuple[str, str]] = []

    for f in files:
        sub = f.parent.name
        name = account_name(f.stem)
        if not NAME_RE.match(name):
            print(f"[skip] 非法账号名 {name!r} <- {f}")
            skipped += 1
            continue
        if name in seen_names:
            print(f"[skip] 重名 {name} <- {f}（已见 {seen_names[name]}）")
            skipped += 1
            continue
        seen_names[name] = str(f)

        raw = f.read_text(encoding="utf-8-sig").strip()
        if not raw:
            print(f"[skip] 空文件 {f}")
            skipped += 1
            continue
        pairs = parse_cookie_string(raw)
        if not any(k == "sessionid" for k, _ in pairs):
            print(f"[warn] {name} 没有 sessionid（{len(pairs)} 条 cookie）")
        if not pairs:
            print(f"[skip] 解析不出 cookie: {f}")
            skipped += 1
            continue

        import hashlib
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()
        if digest in seen_md5:
            print(f"[skip] cookie 内容重复 {name} == {seen_md5[digest]}")
            skipped += 1
            continue
        seen_md5[digest] = name

        dest_dir = Path("accounts") / name
        dest = dest_dir / "cookies.json"
        if dest.exists() and not args.force:
            skipped += 1
            continue
        if args.dry_run:
            imported += 1
            continue
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            payload = [{"name": k, "value": v} for k, v in pairs]
            dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            imported += 1
            notes.append((name, f"ck/{sub}"))
        except Exception as e:
            print(f"[fail] {name}: {type(e).__name__}: {str(e)[:120]}")
            failed += 1

    print(f"\n导入 {imported}，跳过 {skipped}，失败 {failed}（共 {len(files)} 个文件）")

    if args.note_dir and notes and not args.dry_run:
        try:
            import sqlite3, time
            conn = sqlite3.connect("pool_usage.db", timeout=30)
            n = 0
            for name, note in notes:
                conn.execute(
                    "INSERT OR IGNORE INTO accounts_meta (name, created_at, note) VALUES (?,?,?)",
                    (name, time.time(), note))
                conn.execute(
                    "UPDATE accounts_meta SET note=? WHERE name=? AND (note IS NULL OR note='')",
                    (note, name))
                n += 1
            conn.commit()
            conn.close()
            print(f"已写 note=ck/<目录> 到 {n} 个账号")
        except Exception as e:
            print(f"[warn] 写 note 失败（不影响导入）: {type(e).__name__}: {str(e)[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
