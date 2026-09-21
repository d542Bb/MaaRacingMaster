# 7-Zip Standalone Console (7za.exe)

- Version: **26.03** (x64)

- Source: `https://www.7-zip.org/` (7-Zip Extra: `extra/x64/7za.exe`; Copyright (C) 1999-2026 Igor Pavlov)

- License: **GNU LGPL v2.1+**（部分代码 BSD 2/3-clause）— see [7za\_License.txt](./7za_License.txt)

- Purpose: release packaging（solid LZMA2 archive 主推档），CI + 本地打包确定性。

- Verified: C1 benchmark 用此版本实测 7z solid 256M = 136.59 MiB（−31.3%）；本文件随仓库入库，保证本地与 CI 参数/版本一致。

This project (MaaRacingMaster) uses part of the 7-Zip program (7za.exe).
7-Zip is released under the GNU LGPL license.
Source code: `https://www.7-zip.org/`

> 合规说明：依据 7-Zip 许可要求，二进制再分发须随附本 README 与 7za\_License.txt；
> 本项目已将二者与 7za.exe 一并入库 `scripts/release/tools/`，并在
> `THIRD_PARTY_LICENSES.md` 的「GUI / 构建工具链」一节登记。

