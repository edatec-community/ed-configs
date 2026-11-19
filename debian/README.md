# 构建和发布 DEB 包

简体中文 · [English](README.en.md)

在 Debian 或 Ubuntu 上安装打包工具后构建二进制包：

```bash
sudo apt-get install build-essential debhelper dpkg-dev
dpkg-buildpackage -us -uc -b
```

构建产物位于上一级目录，文件名为 `ed-bsp-config_<version>_all.deb`。

发布时将 `debian/changelog` 和 `usr/lib/ed-config/edconfig/__init__.py` 更新至同一版本，再推送对应的 Git 标签（如 `v1.0.0`）。GitHub Actions 核对三个版本号，并将 DEB 上传至 GitHub Release。
