# ed-config

`ed-config` 用于根据 EDATEC 产品型号配置设备。

## 安装

从 [GitHub Releases](https://github.com/edatec-community/ed-configs/releases) 下载对应版本的 DEB 包。
`v1.0.0` Release 发布后，可用以下命令下载并安装初始版本：

```bash
curl -fLO https://github.com/edatec-community/ed-configs/releases/download/v1.0.0/ed-config_1.0.0_all.deb
sudo apt install ./ed-config_1.0.0_all.deb
```

如果设备无法访问 GitHub，可在能访问的电脑上下载该包并复制到设备，然后执行上面的本地安装命令。
也可在具备所需依赖的情况下使用 `sudo dpkg -i ./ed-config_1.0.0_all.deb`。

安装软件包后，即可运行 `ed-config`。本包适用于 Python 3.11 或更新版本的 Debian 系统。

## 查看支持的型号

```bash
ed-config list
```

输出包含全部支持的具体型号和变种型号。

## 应用型号配置

```bash
sudo ed-config apply -m IPC2130
sudo ed-config apply -m IPC1000
sudo ed-config apply -m IPC1100
sudo ed-config apply -m IPC1200
sudo ed-config apply -m IPC1220
sudo ed-config apply -m IPC1220 -l 4g
sudo ed-config apply -m GWL2010 -L 470
```

型号名称不区分大小写，下面的命令效果相同：

```bash
sudo ed-config apply -m ipc2130
```

可选功能：

```text
-w, --wifi                 启用 Wi-Fi
-c, --camera               启用摄像头
-d, --ddr                  声明 IPC2210/IPC2220 安装 DDR，并处理检测到的 SATA USB 存储设备
-l, --lte 4g|5g            声明设备安装的 LTE 硬件类型
-L, --lora 470|868|915     选择 GWL2010/GWL2110 的 LoRa 频段
-s, --supercapacitor       声明设备包含超级电容
-i, --install-tools        安装此配置系列声明的基础工具DEB包
-y, --yes                  确认安装工具包时不再询问
```

例如：

```bash
sudo ed-config apply -m IPC2130 -w -c
sudo ed-config apply -m IPC2130 -l 4g
sudo ed-config apply -m IPC2130 -w -c -l 4g -s
sudo ed-config apply -m IPC2210 -d
sudo ed-config apply -m IPC2500
sudo ed-config apply -m IPC1200 -l 4g
sudo ed-config apply -m GWL2010 -L 470
sudo ed-config apply -m GWL2110 -L 915
sudo ed-config apply -m IPC2130 -i
sudo ed-config apply -m IPC2130 -i -y
```

每次执行都以本次参数为最终配置。没有指定的可选功能会保持关闭。

`-d`只接受IPC2210和IPC2220，只控制运行时配置，不改变`PRODUCT`编码。检测到SATA USB存储设备时，工具读取设备VID/PID并将对应的`usb-storage.quirks=<VID>:<PID>:u`写入cmdline；不会固定写入某个设备ID。

`-s`只用于准确生成产品编码，不会使能硬件模块。`-l`声明设备上安装的LTE硬件并生成产品编码。

`-L`只接受GWL2010和GWL2110；选择频段会自动安装对应的`ed-gw1302-470`、`ed-gw1302-868`或`ed-gw1302-915`包，不改变`PRODUCT`编码。

`dtparam=ant2`由Wi-Fi选项`-w`控制。

`tool-packages.toml`按DEB包名集中登记安装目标，可分别绑定Family、具体型号和硬件选项。Family及型号绑定由`-i`触发；选项绑定随对应参数自动生效。

`-l 4g`或`-l 5g`会自动选择`ed-clte`，`-s`会自动选择`ed-lvd`，无需同时指定`-i`。

`-L`会自动选择对应的LoRa频段包，无需同时指定`-i`


本次实际安装了工具包，或配置变更涉及需要重启的项目时，会提示重启。包已安装且没有需要重启的配置变化时，不会要求重启。

受管服务通过systemd启用链接加入下次启动；应用完成后按命令提示重启，当前会话不会自动启动服务。

如果输出提示需要重启，请执行：

```bash
sudo reboot
```

## 查看状态

```bash
ed-config status
```

状态包括：

- 当前是否已经配置；
- 当前型号、产品编码和可选功能；
- 最近一次配置操作成功或失败；
- 是否需要重启。

## 查看版本和帮助

```bash
ed-config version
ed-config --help
ed-config apply --help
```

所有公开命令都支持 `-j` 或 `--json`，可供程序以 JSON 格式读取结果；调用方还应
同时检查命令退出码。
