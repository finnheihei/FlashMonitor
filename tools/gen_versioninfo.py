"""用 flashmonitor/__init__.py 里的 APP_VERSION 生成 PyInstaller 的 versioninfo.txt（版本号只在那里写一次）。

python tools/gen_versioninfo.py            写 versioninfo.txt
python tools/gen_versioninfo.py --check    只检查现有文件是不是最新的（不一致返回 1）
python tools/gen_versioninfo.py --tag v0.9.0  检查发布的 tag 和版本号一致（release.yml 用）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flashmonitor import APP_ID, APP_NAME, APP_VERSION  # noqa: E402
from flashmonitor.parsing import parse_version  # noqa: E402

OUT = ROOT / "versioninfo.txt"


def render(version: str = APP_VERSION) -> str:
    major, minor, patch = parse_version(version)
    nums = f"({major}, {minor}, {patch}, 0)"
    text = f"""# UTF-8
# 由 tools/gen_versioninfo.py 生成，版本号改 flashmonitor/__init__.py 里的 APP_VERSION
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={nums},
    prodvers={nums},
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
    ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        u'080404B0',
        [StringStruct(u'CompanyName', u'finnheihei'),
        StringStruct(u'FileDescription', u'{APP_NAME}'),
        StringStruct(u'FileVersion', u'{version}'),
        StringStruct(u'InternalName', u'{APP_ID}'),
        StringStruct(u'LegalCopyright', u'Copyright (c) finnheihei'),
        StringStruct(u'OriginalFilename', u'{APP_ID}.exe'),
        StringStruct(u'ProductName', u'{APP_NAME}'),
        StringStruct(u'ProductVersion', u'{version}')])
      ]),
    VarFileInfo([VarStruct(u'Translation', [2052, 1200])])
  ]
)
"""
    return text.replace("\n", "\r\n")


def main(argv: list[str] | None = None) -> int:
    # 控制台输出只用 ASCII：GitHub 的 Windows runner 上 stdout 是 cp1252，打印中文会直接崩
    parser = argparse.ArgumentParser(description="Generate versioninfo.txt from flashmonitor.APP_VERSION")
    parser.add_argument("--check", action="store_true", help="only check that versioninfo.txt is up to date")
    parser.add_argument("--tag", default="", help="check that a release tag (v1.2.3 or refs/tags/v1.2.3) matches APP_VERSION")
    args = parser.parse_args(argv)
    if args.tag:
        tag = args.tag.removeprefix("refs/tags/")
        if tag.lstrip("vV") != APP_VERSION:
            print(f"tag {tag!a} does not match APP_VERSION {APP_VERSION}", file=sys.stderr)
            return 1
        print(f"tag {tag!a} matches APP_VERSION {APP_VERSION}")
        return 0
    want = render()
    if args.check:
        have = OUT.read_bytes().decode("utf-8") if OUT.is_file() else ""
        # git 的 autocrlf 设置不同，签出来可能是 LF 也可能是 CRLF：只比内容
        if have.replace("\r\n", "\n") != want.replace("\r\n", "\n"):
            print("versioninfo.txt is out of date: run python tools/gen_versioninfo.py", file=sys.stderr)
            return 1
        print(f"versioninfo.txt is up to date ({APP_VERSION})")
        return 0
    OUT.write_bytes(want.encode("utf-8"))
    print(f"wrote {OUT.name} ({APP_VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
