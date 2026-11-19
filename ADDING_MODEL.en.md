# Adding a model

[简体中文](ADDING_MODEL.md) · English

This guide uses the CM4-based `IPC2730` as an example. The ordering table can already parse it, and the Catalog still needs an entry. Confirm the mainboard and expansion-board identities, CAN FD configuration, and required software packages before implementation.

| File | Changes |
| --- | --- |
| [hardware/codes.toml](usr/lib/ed-config/catalog/hardware/codes.toml) | Reuse existing platform and hardware codes. Assign a new code only for a new hardware identity and preserve assigned values. |
| [models.toml](usr/lib/ed-config/catalog/models.toml) | Add a concrete model with `name`, `config`, `platform`, `board`, `subboard`, and `screen`. Check that its base `PRODUCT` is unique. |
| [families/](usr/lib/ed-config/catalog/families/) | Reuse an existing directory if its configuration matches. For a dedicated configuration, create `ipc2730/config.toml` and specify the platform, boot settings, and required udev rules. |
| [tool-packages.toml](usr/lib/ed-config/catalog/tool-packages.toml) | Register model packages under `models`, shared packages under `families`, and hardware option packages under `options`. |

The example below assumes IPC2730 has a dedicated mainboard and no expansion board or screen. Use the confirmed hardware identities for the final entry.

```toml
[[models]]
name = "IPC2730"
config = "ipc2730"
platform = "CM4"
board = "IPC2730"
subboard = "None"
screen = "None"
```

**Ordering codes.** IPC2730 already has a parser rule, and its name maps directly to the model. After adding the model, verify that `ED-IPC2730-143200-4CN` forwards `ed-config apply -m IPC2730 -i -l 4g -w`. Update the submodule CSV when the ordering format changes. Update `usr/sbin/ed-config-order` when mapping an alias or a new field.

**Acceptance checks.** Freeze a baseline as described in `AGENTS.md`. After the change, update the expected configuration, model and package selection tests, and ordering entry point tests under `.local-maintenance/tests/`. Verify that the new model appears in `list`, its product code, package list, and configuration plan match the hardware, and existing models match the baseline. Verify CAN FD on the device.

Run these commands from the outer maintenance workspace.

```sh
(cd ed-configs && ./ed-config _validate -j)
python3 -m unittest discover -s .local-maintenance/tests -v
sh -n ed-configs/ed-config
sh -n ed-configs/usr/sbin/ed-config
```
