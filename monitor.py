"""Flash Monitor 入口：源码运行 `python monitor.py`，打包版的入口脚本也是它。

代码都在 flashmonitor 包里，从 flashmonitor/app.py 的 main() 开始看。
开机启动项和快捷方式（源码运行时）指向这个文件，所以它留在项目根目录。
"""

from flashmonitor.app import main

if __name__ == "__main__":
    raise SystemExit(main())
