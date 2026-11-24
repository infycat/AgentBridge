# Third-party components

[繁體中文首頁](../README.md) · [English README](../README.en.md)

AgentBridge uses Python libraries listed in [requirements.txt](../requirements.txt). The Windows archive includes a Python runtime, PySide6, and Qt WebEngine. The archive's `licenses/` directory contains notices for bundled components.

- [Python licence](https://docs.python.org/3/license.html)
- [Qt licensing](https://doc.qt.io/qt-6/licensing.html) and [Qt for Python licences](https://doc.qt.io/qtforpython-6/licenses.html)
- [Qt WebEngine and its Chromium licences](https://doc.qt.io/qtforpython-6/overviews/qtwebengine-licensing.html)
- [PyInstaller licence and bootloader exception](https://pyinstaller.org/en/stable/license.html)
- [Bundled markdown-it licence](../web/assets/vendor/markdown-it-15.0.3.LICENSE.txt)

Vendor components retain their respective licences.

## Qt in the Windows archive

Qt and PySide6 are bundled as shared libraries under their applicable open-source terms. The matching [GitHub Release](https://github.com/infycat/AgentBridge/releases) hosts the corresponding Qt and PySide6 source archives alongside the Windows app. The Qt WebEngine sources include Chromium's license notices.

To run against your own compatible build, extract the Windows archive to a writable directory. Build the matching Qt and PySide6 sources for x64 using the build instructions in those sources. With AgentBridge closed, replace the corresponding DLLs and, if needed, the matching PySide6 or Shiboken extensions, plugins, and WebEngine helper resources under `AgentBridge/_internal/`. Retain their directory layout. The executable loads these shared libraries from that extracted directory.
