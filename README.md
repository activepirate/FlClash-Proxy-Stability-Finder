# FlClash Proxy Delay Recorder

这是独立于 FlClash 的本机代理延迟记录工具。测试结果只在运行期间保存在内存中，并显示各代理的成功率、平均延迟和抖动；不修改 FlClash 项目，也不需要第三方 Python 包。

## 目录

- `app/`：采集逻辑、命令行入口和本机 Web 界面。
- `config/config.json`：本地控制器地址和密钥配置，已加入 Git 忽略规则。
- `config/config.example.json`：不含密钥的配置模板。
- `scripts/`：启动脚本。

## 准备

FlClash 中启用外部控制器并使用本机回环地址（默认 `http://127.0.0.1:9090`）。Web 界面提供 `https://api.ipify.org/`、`https://ipapi.co/json/` 和 `https://ipwho.is/` 三个出口检测按钮；每次点击只直接访问对应服务，并在概览区按行显示公网出口 IP、国家、城市、时区、UTC 偏移以及协议版本和检测时间。另有“服务状态”模块，默认检查 Google、GitHub 和百度，也可以在页面中添加或删除自定义名称和 URL；服务列表会保存到本地 `config/config.json`，以普通 HTTP 请求显示可达状态与请求耗时。不调用 FlClash 代理延迟接口，也不展示代理节点。请求遵循运行环境的常规网络配置（例如系统或环境代理）。在 Web 界面的“控制器密钥”字段输入密钥，保存后会写入本地 `config/config.json`。也可以直接编辑该 JSON 文件中的 `secret`。配置文件按当前系统支持的方式限制为仅当前用户可读写；密钥以明文保存在本机，请勿分享该文件。不要将控制器暴露到局域网或公网。

Python 3.10 或更新版本即可运行，无需安装第三方 Python 包或 Tkinter。浏览器会打开本机 Web 页面，服务仅监听 `127.0.0.1`。

## Web 界面

```bash
./scripts/start.sh
```

启动脚本会运行本机服务并打开浏览器（默认 `http://127.0.0.1:8765`）。界面可设置控制器地址、密钥、测试 URL、轮数、间隔、同时测试数和单次超时；在代理列表中勾选多个代理后，每轮会按“同时测试数”并发测试。页面还显示 `/traffic`、`/logs`、`/connections` 的实时数据，不请求或展示 `/configs`。开始采集后逐项显示结果与实时汇总，数据只保存在内存中，支持停止。按 `Ctrl+C` 关闭服务。

控制器状态区的 `/traffic` 显示控制器全局实时上下行速度和累计量，`/connections` 提供活动连接及各连接累计字节数。代理/直连分类基于活动连接的 `chains`：纯 `DIRECT`、`PASS`、`COMPATIBLE` 链归为直连，含代理节点的链归为代理。分类速度由约每秒轮询的连接计数差分换算，分类累计从本次监控开始计算；监控启动前流量、轮询间已结束的短连接和无法识别的链路不计入分类值，因此不等同于控制器启动以来的精确分类总量。

如需手动打开浏览器，或希望服务不自动打开浏览器：

```bash
./scripts/start.sh --no-browser
```

## 命令行

```bash
./scripts/start.sh --cli
./scripts/start.sh --cli --rounds 30 --interval 10
./scripts/start.sh --cli --proxy HK --proxy Tokyo --rounds 20
FLCLASH_SECRET='你的控制器密钥' ./scripts/start.sh --cli
```

命令行也会读取 `config/config.json`；`FLCLASH_SECRET` 环境变量优先于配置中的密钥。默认进行 5 轮测试，每轮间隔 5 秒，同时测试 20 个代理，结果只在本次运行内存中保留。也可以使用 `--concurrency 3` 指定并发数。默认跳过 `DIRECT`、`REJECT` 等内置代理及 Selector、URLTest、LoadBalance、Relay 策略组；策略组不会参与测试。`--proxy` 按代理名称做不区分大小写的包含匹配。测试 URL 默认为 `https://www.gstatic.com/generate_204`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
