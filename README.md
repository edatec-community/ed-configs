# ed-config

`ed-config` 用于根据 EDATEC 产品型号配置设备。

## 查看支持的型号

```bash
ed-config list
```

输出包含全部支持的具体型号和变种型号。

## 应用型号配置

```bash
sudo ed-config apply -m IPC2130
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
-s, --supercapacitor       声明设备包含超级电容
```

例如：

```bash
sudo ed-config apply -m IPC2130 -w -c
sudo ed-config apply -m IPC2130 -l 4g
sudo ed-config apply -m IPC2130 -w -c -l 4g -s
sudo ed-config apply -m IPC2210 -d
sudo ed-config apply -m IPC2500
```

每次执行都以本次参数为最终配置。没有指定的可选功能会保持关闭。

`-d`只接受IPC2210和IPC2220，只控制运行时配置，不改变`PRODUCT`编码。检测到SATA USB存储设备时，工具读取设备VID/PID并将对应的`usb-storage.quirks=<VID>:<PID>:u`写入cmdline；不会固定写入某个设备ID。

`-l`和`-s`只用于准确生成产品编码，不会加载驱动、增加启动配置或使能硬件模块。

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
