# 按订购编码配置

简体中文 · [English](ORDER_CODES.en.md)

安装 DEB 后，使用 `sudo ed-config-order 'ED-HMI3120-070CC-10216-4CN-S'`
按订购编码配置。省略编码可使用键盘式扫码器，或从标准输入读取扫码文本。
入口默认传入 `-i`，有缺包时使用原有安装确认。指定 `-y` 会跳过安装确认。
订单文本末尾可用空格或换行附加 `ES` 和九位数字组成的序列号，入口将其显示在标准错误。

支持转换 Wi-Fi、摄像头、4G、超级电容，IPC2500 的 `5GL` 标记，以及
GWL 的 CN470、EU868、US915。会列出当前未应用的已知订单规格，例如 DDR、
eMMC、存储和镜头参数。无法解析的编码、未支持的型号或未知配置字段会报错。

源码可运行 `./ed-config-order`，缺少解析器时会初始化固定版本的 Git 子模块。
本地打包前运行 `git submodule update --init`。DEB 已包含解析器和数据，
解析订购编码时无需网络。

`ED-CM0NANO-…` 作为 `IPC1000` 的订购编码别名，使用同一产品编码。
`ED-CM4MED10-…` 映射到 `CM4MEDIA`。
`ED-HMI2002-116CA-…` 映射到 `HMI2002_116C`，默认的 `-i` 选择 `ed-ddcci-mib-tool`。
HMI3320/3321 的 156C 编码映射到各自的具体型号。

目前只显示以下订单字段及其值，设备配置由上文列出的选项决定。

| 字段用途 | 字段名 |
| --- | --- |
| 内存与存储 | `ddr`、`pi4_ddr`、`ddr_emmc`、`ddr_ssd`、`combined_memory_token`、`emmc`、`sd`、`ssd`、`storage` |
| 镜头和成像 | `focal`、`color`、`polarizer` |
| 机身、电源和扩展 | `case`、`cooling`、`heatsink`、`power_connector`、`poe`、`hailo_or_ssd`、`tpm` |
| 其他订购规格 | `audio`、`codesys`、`region`、`rs232_count`、`rs485_count`、`serial`、`suffix` |

IPC2210 和 IPC2220 的 SATA 设置仍可通过原有 `ed-config apply -d` 手动指定。
订购编码目前未标记该设置。

当前解析表的 57 个示例都能匹配，其中 42 个可转换为配置命令。其余 15 个示例
涉及下列暂不支持的订购型号，运行时会报错。

- AIC1000、AIC3000、AIC3000-200、AIC3100、CLAWBOX
- CM4IO
- IPC2700、IPC3030、IPC2642、PAC3020、PAC3110、PAC3630、TPM、XGW

当前 HMI2320 和 HMI2321 的订购编码解析表只列出 121C、156C 和 215C。
