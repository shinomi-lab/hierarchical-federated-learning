import json
from pathlib import Path
from typing import Set, List
import logging

logger = logging.getLogger(__name__)


def _list_round_dirs(base_dir: Path) -> List[Path]:
    if not base_dir.exists():
        return []
    dirs = [p for p in base_dir.iterdir() if p.is_dir() and p.name.startswith('r')]
    # keep only those whose suffix after 'r' is an int
    def round_num(p: Path):
        try:
            return int(p.name[1:])
        except Exception:
            return -1
    dirs = [d for d in dirs if round_num(d) >= 0]
    dirs.sort(key=round_num)
    return dirs


def cleanup_received_dirs_and_markers(
    base_dir: Path,
    received_by_edge_file: Path,
    received_hashes_file: Path,
    keep_recent_rounds: int = 3,
    dry_run: bool = False,
) -> dict:
    """
    Clean up old received_edges data and corresponding markers.

    Strategy:
    - Keep only the most recent `keep_recent_rounds` directories under base_dir named r{round}.
    - Remove older r{round} directories from disk (or report when dry_run=True).
    - Prune keys in received_by_edge mapping for rounds that were removed.
    - Recompute received_hashes.json from remaining meta files (content_sha256 fields) to avoid stale SHAs.

    Returns a summary dict with lists of removed rounds and updated marker paths.
    """
    summary = {
        "kept_rounds": [],
        "removed_rounds": [],
        "received_by_edge_pruned": False,
        "received_hashes_rebuilt": False,
    }

    base_dir = Path(base_dir)
    received_by_edge_file = Path(received_by_edge_file)
    received_hashes_file = Path(received_hashes_file)

    round_dirs = _list_round_dirs(base_dir)
    if not round_dirs:
        logger.info("ラウンド用ディレクトリが見つかりません: %s", base_dir)
        return summary

    # decide which to keep
    keep = round_dirs[-keep_recent_rounds:]
    keep_names = set(p.name for p in keep)
    removed = [p for p in round_dirs if p.name not in keep_names]

    summary["kept_rounds"] = [p.name for p in keep]
    summary["removed_rounds"] = [p.name for p in removed]

    # remove old directories (optimize by listing children once)
    for rdir in removed:
        if dry_run:
            logger.info("[dry-run] 次のディレクトリを削除予定: %s", rdir)
            continue

        try:
            # simple lockfile to avoid concurrent cleanup runs
            lockfile = rdir.parent / f".cleanup_lock_{rdir.name}"
            if lockfile.exists():
                logger.warning("ロックファイルが存在するためスキップします（同時クリーンアップ回避）: %s", rdir)
                continue
            try:
                lockfile.write_text(str(__import__('os').getpid()), encoding='utf-8')
            except Exception:
                # if we cannot write lock, continue but log
                logger.warning("ロックファイルを作成できませんでした。注意して続行します: %s", lockfile)

            # collect all children once
            children = list(rdir.rglob('*'))
            # delete files first
            for child in children:
                try:
                    if child.is_file():
                        child.unlink()
                except Exception:
                    logger.exception("ファイル削除に失敗しました %s", child)
            # then attempt to remove directories bottom-up
            for sub in sorted(children, key=lambda p: len(p.parts), reverse=True):
                try:
                    if sub.is_dir():
                        sub.rmdir()
                except Exception:
                    pass
            # finally remove the round dir itself
            try:
                rdir.rmdir()
            except Exception:
                # best-effort: ignore
                pass

            # remove lockfile
            try:
                if lockfile.exists():
                    lockfile.unlink()
            except Exception:
                pass

            logger.info("古いラウンドディレクトリを削除しました %s", rdir)
        except Exception:
            logger.exception("ラウンドディレクトリの削除に失敗しました %s", rdir)

    # prune received_by_edge
    try:
        if received_by_edge_file.exists():
            raw = received_by_edge_file.read_text(encoding='utf-8')
            mapping = json.loads(raw)
            if isinstance(mapping, dict):
                orig_keys = set(mapping.keys())
                keep_keys = set([k for k in orig_keys if ('r' + str(k) if isinstance(k, int) else k) in keep_names or (('r' + k) in keep_names)])
                # mapping keys are probably numeric strings like '25' or 'r25' depending on earlier code; normalize
                def key_to_roundname(k):
                    s = str(k)
                    if s.startswith('r'):
                        return s
                    return 'r' + s

                new_map = {}
                for k, v in mapping.items():
                    rn = key_to_roundname(k)
                    if rn in keep_names:
                        # keep
                        new_map[str(int(rn[1:]))] = v

                if dry_run:
                    logger.info("[dry-run] 次をキー %s で書き換え予定: %s", list(new_map.keys()), received_by_edge_file)
                else:
                    # write a bak copy first
                    try:
                        if received_by_edge_file.exists():
                            bak = received_by_edge_file.with_suffix('.bak')
                            received_by_edge_file.replace(bak)
                    except Exception:
                        # if bak fails, continue
                        pass
                    tmp = received_by_edge_file.with_suffix('.tmp')
                    tmp.write_text(json.dumps(new_map, indent=2, ensure_ascii=False), encoding='utf-8')
                    # atomic replace
                    tmp.replace(received_by_edge_file)
                    summary["received_by_edge_pruned"] = True
    except Exception:
        logger.exception("received_by_edge の整理に失敗しました %s", received_by_edge_file)

    # rebuild received_hashes from remaining meta files
    try:
        shas: Set[str] = set()
        for kept in keep:
            for meta_file in kept.rglob('*.meta.json'):
                try:
                    j = json.loads(meta_file.read_text(encoding='utf-8'))
                    sha = j.get('content_sha256') or j.get('sha256')
                    if sha:
                        shas.add(sha)
                except Exception:
                    # ignore parse errors for single meta files
                    pass

        if dry_run:
            logger.info("[dry-run] %d 件の SHA を %s へ書き込み予定", len(shas), received_hashes_file)
        else:
            tmp = received_hashes_file.with_suffix('.tmp')
            tmp.write_text(json.dumps(sorted(list(shas)), indent=2, ensure_ascii=False), encoding='utf-8')
            tmp.replace(received_hashes_file)
            summary["received_hashes_rebuilt"] = True
    except Exception:
        logger.exception("保持ラウンドから received_hashes の再構築に失敗しました")

    return summary


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Cleanup received_edges directories and marker files')
    parser.add_argument('--base-dir', default='received_edges', help='base dir for received edges')
    parser.add_argument('--received-by-edge', default='received_edges/received_by_edge.json', help='path to received_by_edge.json')
    parser.add_argument('--received-hashes', default='received_edges/received_hashes.json', help='path to received_hashes.json')
    parser.add_argument('--keep', type=int, default=3, help='number of recent rounds to keep')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    res = cleanup_received_dirs_and_markers(
        base_dir=Path(args.base_dir),
        received_by_edge_file=Path(args.received_by_edge),
        received_hashes_file=Path(args.received_hashes),
        keep_recent_rounds=args.keep,
        dry_run=args.dry_run,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
