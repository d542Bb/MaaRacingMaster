#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成可选模型包（深度几何 + 线观测，DLC）。

内容：三个 ONNX 权重 + 许可/安装说明 README + SHA256SUMS，打成一个 zip。
发布包默认不含权重；用户下载本包后解压到
``%APPDATA%\\MaaRacingMaster\\data\\speedrush\\models\\`` 即可使用深度几何与线观测。
线观测受 BDD100K 非商业/科研许可约束，构建时会一并打入
``LICENSE-BDD100K.txt``；源文件或许可缺失时本脚本直接失败。

用法：
    python scripts/release/make_model_pack.py --version 0.30.0-dev.1
    python scripts/release/make_model_pack.py --version X --out build/release

生成前会用 ``model_assets.MODEL_MANIFEST`` 校验源文件（尺寸 + SHA256），
不匹配直接失败，避免把损坏/错版权重发给用户。
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = REPO_ROOT / "maaracing_master" / "plugins" / "speedrush" / "resources" / "models"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from maaracing_master.plugins.speedrush import model_assets  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(version: str, out_dir: Path) -> tuple[Path, Path]:
    version = version[1:] if version.startswith("v") else version
    rels = list(model_assets.MODEL_MANIFEST)
    files: list[tuple[str, Path]] = []
    sums: list[str] = []
    for rel in rels:
        src = model_assets.MODELS_DIR / model_assets.packed_rel(rel)   # 仓库内真源
        packed = model_assets.packed_rel(rel)
        err = model_assets.verify(src, rel)
        if err:
            raise SystemExit(f"模型包源文件不可用：{err}")
        files.append((packed, src))
        sums.append(f"{_sha256(src)}  {packed}")

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"MaaRacingMaster-speedrush-models-{version}.zip"
    readme = MODELS_DIR / "README.md"
    license_file = MODELS_DIR / "LICENSE-BDD100K.txt"
    if not license_file.is_file():
        raise SystemExit(f"模型包许可文件缺失：{license_file}")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for packed, src in files:
            zf.write(src, packed)
        if readme.is_file():
            zf.write(readme, "README.md")
        zf.write(license_file, "LICENSE-BDD100K.txt")
        zf.writestr("SHA256SUMS", "\n".join(sums) + "\n")

    digest = _sha256(zip_path)
    sha_path = Path(str(zip_path) + ".sha256")
    sha_path.write_text(f"{digest}  {zip_path.name}\n", encoding="ascii")
    return zip_path, sha_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成可选模型包（深度 + 线观测）")
    parser.add_argument("--version", required=True, help="版本号（可带 v 前缀）")
    parser.add_argument("--out", default=str(REPO_ROOT / "build" / "release"),
                        help="输出目录（默认 build/release）")
    args = parser.parse_args(argv)
    zip_path, sha_path = build(args.version, Path(args.out))
    print(f"[model-pack] zip: {zip_path}")
    print(f"[model-pack] sha256: {sha_path.read_text(encoding='ascii').strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
