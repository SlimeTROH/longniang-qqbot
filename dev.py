"""开发模式启动器：监视插件代码变化，自动重启机器人。

用法（在 qqbot 目录下）：
    ..\\venv\\Scripts\\python.exe dev.py

和正式启动的区别：
    - 正式：双击「一键启动.cmd」→ 起 NapCat + 机器人，改代码要手动重启
    - 开发：跑这个脚本 → 改完 .py 保存，机器人自动重启（1 秒内）

关键点：只监视「代码文件」，忽略日志/缓存/状态文件，
否则机器人自己写日志就会触发重启 → 无限重启循环。
"""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 要监视的路径（文件或目录）
WATCH = [HERE / "plugins", HERE / "bot.py"]

# 这些目录/文件的变化一律忽略（它们是机器人自己运行产生的）
IGNORE_PARTS = {"__pycache__", "logs", "state", ".qq-outbox", ".qq-inbox", ".sent", ".git"}
# 只有这些后缀的变化才值得重启
WATCH_SUFFIX = {".py", ".json", ".toml", ".env"}


def snapshot() -> dict:
    """拍一张快照：所有"代码文件" -> 修改时间。"""
    state: dict[str, int] = {}
    for root in WATCH:
        if not root.exists():
            continue
        if root.is_file():
            state[str(root)] = root.stat().st_mtime_ns
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            # 忽略运行产物
            if any(part in IGNORE_PARTS for part in path.parts):
                continue
            if path.suffix not in WATCH_SUFFIX and path.name != ".env":
                continue
            try:
                state[str(path)] = path.stat().st_mtime_ns
            except OSError:
                pass
    return state


def start_bot() -> subprocess.Popen:
    print("[dev] 启动机器人 ...\n")
    return subprocess.Popen([sys.executable, str(HERE / "bot.py")], cwd=str(HERE))


def stop_bot(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> None:
    print("=" * 56)
    print("  开发模式：改完代码自动重启（Ctrl+C 退出）")
    print("=" * 56)
    for w in WATCH:
        print(f"  监视: {w}")
    print()

    proc = start_bot()
    last = snapshot()

    try:
        while True:
            time.sleep(1)                       # 每秒检查一次

            current = snapshot()
            if current != last:                 # 有文件变化
                changed = [
                    Path(k).name
                    for k in set(current) | set(last)
                    if current.get(k) != last.get(k)
                ]
                print(f"[dev] 检测到变化：{', '.join(changed[:6])}")
                stop_bot(proc)
                proc = start_bot()
                last = snapshot()
                continue

            # 机器人自己崩了也自动拉起来（方便调试语法错误）
            if proc.poll() is not None:
                print(f"[dev] 机器人已退出（退出码 {proc.returncode}），2 秒后重启 ...\n")
                time.sleep(2)
                proc = start_bot()
                last = snapshot()

    except KeyboardInterrupt:
        print("\n[dev] 收到退出信号 ...")
    finally:
        stop_bot(proc)
        print("[dev] 已停止。")


if __name__ == "__main__":
    main()
