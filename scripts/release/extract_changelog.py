#!/usr/bin/env python3
"""从 docs/update_log.md 抽取指定版本小节，作为 GitHub Release 正文的信源。

对外叙事层是人工撰写的修改日志，git DAG 是内部施工日志：Release 面向玩家，
因此正文只取日志对应小节，不把 commit 标题直倒给用户。

用法：
    python scripts/release/extract_changelog.py <version> [--log PATH]

    version 不带前缀 v（如 0.21.0-dev.2），与 release.yml 的 version 输出一致。

输出与退出码：
    命中小节 → 小节 markdown 写 stdout（首行为该版本标题的加粗摘要），退出码 0
    未命中   → stdout 为空，stderr 提示，退出码 0（交由调用方回退，不阻断发布）
    日志缺失 → stderr 报错，退出码 1（真实故障，与「尚未补写小节」区分）
"""

import argparse
import sys
from pathlib import Path

VERSION_HEADING = "### "
DATE_HEADING = "## "


def extract(log_text, version):
    """返回 version 对应小节的 markdown 文本，未命中返回空串。

    版本标题按 `### v<version> <标题>` 约定，以空格精确切分首字段：
    等值比较（非前缀匹配）才能避免 `v0.19.0` 误吞 `v0.19.0-dev.1`。
    日志按时间倒序且历史存在同名版本标题，故只取首个命中。
    """
    want = "v" + version
    out = []
    printing = False
    done = False
    for line in log_text.splitlines():
        if line.startswith(VERSION_HEADING):
            fields = line[len(VERSION_HEADING):].split(" ", 1)
            heading = fields[0]
            title = fields[1].strip() if len(fields) > 1 else ""
            if heading == want and not done:
                done = True
                printing = True
                if title:
                    out.append("**{}**".format(title))
                continue
            printing = False
            continue
        if line.startswith(DATE_HEADING):
            printing = False
            continue
        if printing:
            out.append(line)
    return "\n".join(out).strip()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="抽取 docs/update_log.md 中指定版本小节作为 Release 正文"
    )
    parser.add_argument("version", help="版本号，不含前缀 v（如 0.21.0-dev.2）")
    parser.add_argument(
        "--log",
        default="docs/update_log.md",
        help="修改日志路径（默认 docs/update_log.md）",
    )
    args = parser.parse_args(argv)

    path = Path(args.log)
    if not path.is_file():
        print("修改日志不存在：{}".format(path), file=sys.stderr)
        return 1

    body = extract(path.read_text(encoding="utf-8"), args.version)
    if not body:
        print(
            "未在 {} 找到 v{} 小节，Release 将回退 git log".format(path, args.version),
            file=sys.stderr,
        )
        return 0

    sys.stdout.write(body + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
