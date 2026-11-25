# Building and releasing the DEB package

[简体中文](PACKAGING.md) · English

Build the binary package on Debian or Ubuntu with the Debian packaging tools installed:

```bash
sudo apt-get install build-essential debhelper dpkg-dev device-tree-compiler
dpkg-buildpackage -us -uc -b
```

The package is written to the parent directory as `ed-bsp-config_<version>_all.deb`.

## Official releases

1. Keep the versions in `debian/changelog`, `usr/lib/ed-config/edconfig/__init__.py`, and the release tag in sync.
2. Ask the project maintainer to designate a Raspberry Pi for each release. On the selected Debian device, run `./ed-config _validate -j` and the build command above using the release commit and its pinned submodule.
3. Copy the DEB back to the publishing workstation and verify its package name, version, architecture, and SHA256 checksum.
4. Push the commit and tag. GitHub Actions validates the Catalog and versions. Upload the Debian-built DEB to the matching GitHub Release, then download it and verify its SHA256 checksum. Use the same Debian-built artifact for the APT repository when applicable.
