# Building and releasing the DEB package

Build the binary package on Debian or Ubuntu with the Debian packaging tools installed:

```bash
sudo apt-get install debhelper dpkg-dev
dpkg-buildpackage -us -uc -b
```

The package is written to the parent directory as `ed-config_<version>_all.deb`.
For a release, keep `debian/changelog` and `usr/lib/ed-config/edconfig/__init__.py`
on the same version, then push a matching tag such as `v1.0.0`. The GitHub Actions
workflow checks all three versions and attaches the DEB package to the GitHub Release.
