# 构建和发布 DEB 包

简体中文 · [English](PACKAGING.en.md)

在 Debian 或 Ubuntu 上安装打包工具后构建二进制包：

```bash
sudo apt-get install build-essential debhelper dpkg-dev device-tree-compiler
dpkg-buildpackage -us -uc -b
```

构建产物位于上一级目录，文件名为 `ed-bsp-config_<version>_all.deb`。

## 正式发布

1. 同步 `debian/changelog`、`usr/lib/ed-config/edconfig/__init__.py` 和发布标签的版本号。
2. 每次发版前向项目负责人确认本次使用的 Raspberry Pi。登录本次指定的 Debian 设备后，用待发布提交及其固定版本的子模块执行 `./ed-config _validate -j` 和上述构建命令。
3. 将 DEB 复制回发布工作站，核对包名、版本、架构及 SHA256。
4. 推送提交和标签。GitHub Actions 校验 Catalog 和版本号。将 Debian 主机生成的 DEB 上传到对应的 GitHub Release，发布后下载并核对 SHA256。需要发布到 APT 源时，使用同一份 Debian 构建产物。
