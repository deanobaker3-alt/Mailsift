"""Writes build/version_info.txt: the Windows "Details" tab for MailSift.exe
(product name, version, copyright). Usage: python tools/make_version_info.py"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from mailsift import __version__  # noqa: E402

OWNER = "Dean Baker"
parts = [int(p) for p in __version__.split(".")] + [0] * 4
v = tuple(parts[:4])

TEMPLATE = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080904B0', [
      StringStruct('CompanyName', '{OWNER}'),
      StringStruct('FileDescription', 'MailSift subscription cleaner'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'MailSift'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 {OWNER}. PolyForm Noncommercial 1.0.0; commercial licence available.'),
      StringStruct('OriginalFilename', 'MailSift.exe'),
      StringStruct('ProductName', 'MailSift'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [2057, 1200])])
  ]
)
"""

out = pathlib.Path("build")
out.mkdir(exist_ok=True)
(out / "version_info.txt").write_text(TEMPLATE, encoding="utf-8")
print(f"wrote build/version_info.txt for MailSift {__version__}")
