# Configure a device from an ordering code

[简体中文](ORDER_CODES.md) · English

After installing the DEB, run `sudo ed-config-order 'ED-HMI3120-070CC-10216-4CN-S'`. Omit the argument to use a keyboard-wedge scanner or read the scan from standard input. The entry point passes `-i` by default and prompts when packages need to be installed. Use `-y` to skip confirmation. A serial number consisting of `ES` and nine digits may follow a space or line break. The entry point displays it on standard error.

Supported fields include Wi-Fi, camera, 4G, supercapacitor, the IPC2500 `5GL` marker, and the GWL regions CN470, EU868, and US915. The entry point displays other known specifications, such as DDR, eMMC, storage, and lens settings. Unrecognized codes, unsupported models, and unknown configuration fields produce an error.

From a source checkout, run `./ed-config-order`. It initializes the pinned Git submodule if the parser is missing. Run `git submodule update --init` before building the package. The DEB includes the parser and data for offline parsing.

- `ED-CM0NANO-…` is an ordering alias for `IPC1000` and uses the same product code.
- `ED-CM4MED10-…` maps to `CM4MEDIA`.
- `ED-HMI2002-116CA-…` maps to `HMI2002_116C`. The default `-i` selects `ed-ddcci-mib-tool`.
- HMI3320 and HMI3321 156C codes map to their respective concrete models.

The supported options above determine the device configuration. The following ordering fields are displayed for reference.

| Purpose | Field names |
| --- | --- |
| Memory and storage | `ddr`, `pi4_ddr`, `ddr_emmc`, `ddr_ssd`, `combined_memory_token`, `emmc`, `sd`, `ssd`, `storage` |
| Lenses and imaging | `focal`, `color`, `polarizer` |
| Enclosure, power, and expansion | `case`, `cooling`, `heatsink`, `power_connector`, `poe`, `hailo_or_ssd`, `tpm` |
| Other specifications | `audio`, `codesys`, `region`, `rs232_count`, `rs485_count`, `serial`, `suffix` |

Use `ed-config apply -d` to manually select the SATA setting for IPC2210 and IPC2220. Their ordering codes do not currently encode this setting.

All 57 examples in the current parser table match, and 42 can be converted to configuration commands. The other 15 examples refer to these unsupported products and return an error.

- AIC1000, AIC3000, AIC3000-200, AIC3100, CLAWBOX
- CM4IO
- IPC2700, IPC3030, IPC2642, PAC3020, PAC3110, PAC3630, TPM, XGW

The HMI2320 and HMI2321 entries in the current ordering table cover 121C, 156C, and 215C.
