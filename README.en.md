# ed-config

[简体中文](README.md) · English

`ed-config` configures EDATEC devices by product model. Maintainers can refer to the [guide to adding models](docs/ADDING_MODEL.en.md) and the [packaging guide](docs/PACKAGING.en.md).

## Installation

Download the DEB from [GitHub Releases](https://github.com/edatec-community/ed-configs/releases). To install version 1.0.2:

```bash
curl -fLO https://github.com/edatec-community/ed-configs/releases/download/v1.0.2/ed-bsp-config_1.0.2_all.deb
sudo apt install ./ed-bsp-config_1.0.2_all.deb
```

If the device cannot reach GitHub, download the package on another computer and copy it to the device. You can also use `sudo dpkg -i ./ed-bsp-config_1.0.2_all.deb` when the required dependencies are already available. The package requires Python 3.11 or later on Debian.

## Supported models

```bash
ed-config list
```

The list includes every supported concrete model and variant. Model names are case-insensitive.

## Apply a configuration

Applying a configuration requires root privileges. Use `sudo` from a regular account.
All models require a running kernel of at least `6.18.50` for `apply`. Use `uname -r` to check the running version.

```bash
sudo ed-config apply -m IPC2130 -w -c
sudo ed-config apply -m IPC2210 -d
sudo ed-config apply -m IPC1220 -l 4g
sudo ed-config apply -m GWL2110 -L 915
sudo ed-config apply -m IPC2130 -i -y
```

| Option | Effect |
| --- | --- |
| `-w, --wifi` | Enable Wi-Fi and select `dtparam=ant2` |
| `-c, --camera` | Enable the camera |
| `-d, --ddr` | Declare DDR on IPC2210/IPC2220 and configure a storage quirk for detected SATA USB hardware |
| `-l, --lte 4g\|5g` | Declare the installed LTE hardware |
| `-L, --lora 470\|868\|915` | Select the SX1302 frequency band for GWL2010/GWL2110 |
| `-s, --supercapacitor` | Declare the installed supercapacitor |
| `-i, --install-tools` | Install tool packages declared for the model or configuration family |
| `-y, --yes` | Skip the package installation prompt |

Each invocation describes the full desired configuration. Managed options omitted from the command are turned off. The DDR option writes a `usb-storage.quirks=<VID>:<PID>:u` entry for the detected device and leaves `PRODUCT` unchanged. LTE and supercapacitor options encode the installed hardware and automatically select `ed-clte` and `ed-lvd`, respectively.

For GWL2010 and GWL2110, `-L` installs the corresponding `ed-gw1302-470`, `ed-gw1302-868`, or `ed-gw1302-915` package without requiring `-i` or changing `PRODUCT`. Apply stops if a different SX1302 band package or a legacy SX1303 package is installed, or if `/etc/ed_gwl/region` conflicts with the selected band.

`tool-packages.toml` defines package bindings. Family and model packages require `-i`, while option packages are selected automatically.

When packages are installed or a configuration change requires a reboot, the command prompts you to reboot. Managed services are enabled for the next boot by systemd links.

```bash
sudo reboot
```

## Configure from an ordering code

```bash
sudo ed-config-order
sudo ed-config-order 'ED-GWL2010-232-EU'
```

You can also pass the complete scanner text on standard input. An optional serial number such as `ES264500508` can follow the code after a space or line break. The command displays the serial number and applies the model and options from the ordering code. GWL codes with CN, EU, or US select 470, 868, or 915 MHz, respectively. See the [ordering-code guide](docs/ORDER_CODES.en.md) for examples.

## Status, version, and help

```bash
ed-config status
ed-config version
ed-config --help
ed-config apply --help
```

Status reports whether the device is configured, its model, product code and options, the result of the latest apply, and whether a reboot is required.

All public `ed-config` commands support `-j` or `--json`. Automation should also check the exit code.
