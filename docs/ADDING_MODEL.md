# 新增型号交接指南

简体中文 · [English](ADDING_MODEL.en.md)

以已登记的 CM4 型号 `IPC2730` 为例。新增型号前，先确认主板、扩展板、外设配置及所需软件包。

| 文件 | 要改什么 |
| --- | --- |
| [hardware/codes.toml](../usr/lib/ed-config/catalog/hardware/codes.toml) | 复用已有平台和硬件编码。新硬件身份才分配新码，保留已分配值。 |
| [models.toml](../usr/lib/ed-config/catalog/models.toml) | 加一条具体型号。填写 `name`、`config`、`platform`、`board`、`subboard`、`screen`，确保基础 `PRODUCT` 唯一。 |
| [families/](../usr/lib/ed-config/catalog/families/) | 配置相同就复用现有目录。有专用配置时，新建 `ipc2730/config.toml`，填平台、启动配置和必要的 udev 规则。 |
| [tool-packages.toml](../usr/lib/ed-config/catalog/tool-packages.toml) | 型号专用包加到 `models`，系列共用包加到 `families`，随硬件选项安装的包加到 `options`。 |

IPC2730 使用 IPC2210 主板和 `S_8X8Y4C-SM` 扩展板，无屏幕。

```toml
[[models]]
name = "IPC2730"
config = "ipc2730"
platform = "CM4"
board = "IPC2210"
subboard = "S_8X8Y4C-SM"
screen = "None"
```

每条 `dtoverlay` 都需要明确来源。系统提供的 overlay 在 [kernel-capabilities.toml](../usr/lib/ed-config/catalog/kernel-capabilities.toml) 按已验证的内核版本登记名称和实际用到的参数。DEB 提供的名称通过 `tool-packages.toml` 的 `overlays` 声明。型号随附 DTS 通过 Family 的 `overlay_fallbacks` 声明，IPC2730 的 `ed-ipc-v2` 是现有示例。

**订购编码。** IPC2730 已有解析规则，名称也能直接对应。可用 `ED-IPC2730-143200-4CN` 验证入口是否转发 `ed-config apply -m IPC2730 -i -l 4g -w`。只有订购格式变了才改子仓库的 CSV，别名或新字段的映射才改 `usr/sbin/ed-config-order`。

**验收。** 按 `AGENTS.md` 先冻结基线。修改后更新 `.local-maintenance/tests/` 下的配置期望、型号与包选择测试、订购入口测试。确认新型号出现在 `list`，产品编码、包清单和配置计划正确，现有型号与基线一致。CAN FD 最后在对应设备上验证。

在维护工作区外层运行：

```sh
(cd ed-configs && ./ed-config _validate -j)
python3 -m unittest discover -s .local-maintenance/tests -v
sh -n ed-configs/ed-config
sh -n ed-configs/usr/sbin/ed-config
```
