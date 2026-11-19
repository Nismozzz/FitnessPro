# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 规格：把后端、前端构建和运行时依赖打成一个目录。"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ("config.yml", "."),
    ("frontend/dist", "frontend/dist"),
]
binaries = []
hidden = collect_submodules("app")
for package in ("uvicorn", "jieba", "tzdata"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hidden += package_hidden

hidden += [
    "aiosqlite",
    "asyncpg",
    "bcrypt",
    "jwt",
    "yaml",
    "dotenv",
    "multipart",
    "tkinter",
    "_tkinter",
]

a = Analysis(
    ["launcher/desktop.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "pytest", "sentence_transformers", "torch"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FitnessPro",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="FitnessPro",
)
