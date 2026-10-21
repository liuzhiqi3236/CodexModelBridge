# 模桥 ModelBridge

选好模型，一键接入 Codex。Windows 本地工具，第一版支持 OpenAI、Claude、Gemini、DeepSeek 官方目录发现，以及 Sub2API 账号密钥选择和 Codex 模型追加。

## 下载

目前仅提供 **Windows 10/11 64 位**可执行版本。

- [下载 Windows 版 v0.1.3](https://github.com/liuzhiqi3236/CodexModelBridge/releases/download/v0.1.3/CodexModelBridge-v0.1.3-windows-x64.zip)
- [查看发布记录和最新版本](https://github.com/liuzhiqi3236/CodexModelBridge/releases/latest)

下载 ZIP 后先完整解压，再双击其中的 `CodexModelBridge.exe`；不要在压缩包内直接运行。`runtime` 和 `sources.json` 是必需文件，请一起保留。下载页的 “Source code” 是源码，普通使用请选择带 `windows-x64` 的 ZIP。

更新时先退出模桥，将新版解压到原来的工具目录，保留自己的 `data`；如果修改过 `sources.json`，也请保留自己的配置。目前不提供 macOS、Linux 或 Windows ARM64 原生可执行包。

## 启动

只需双击 `CodexModelBridge.exe`。文件夹外层只有这一个启动程序；`runtime` 是程序自动使用的内部组件，不需要打开。请保留完整文件夹。需要 Windows 10/11、Microsoft Edge WebView2 和已安装的 Codex；不需要自行安装 Python。

主程序文件夹内的 `data` 用来保存设置、官方目录缓存、加密凭据和加密备份。移动工具文件夹后，如已将 Codex 连接切换到模桥，需要重新应用连接，更新凭据助手的位置。

## 使用

1. 填写你平时使用的 Sub2API 站点地址，`http://` 和 `https://` 都可以，保留原来的端口和部署路径，点击“打开站点登录”。HTTP 会提示连接未加密，但不会拦截。在站点原有页面完成登录；验证码、二次验证按站点页面处理。
2. 登录成功后，模桥读取当前账号的全部 API Key。选择一个 Key 后自动读取模型；失败会显示原因，可点击“重试读取模型”。换 Key 不会修改 Codex 连接。
3. 点击“联网刷新模型”，新发现的模型会带标记并排在前面。点击顶部厂商卡片筛选，点击“全部模型”恢复全部厂商，搜索框可按名称或 ID 查找。模型显示“等待参数”时，点击旁边的“处理”，按提示登录、选 Key、重试或导入参数文件。支持按准确 ID 手动添加名称。
4. 勾选参数已就绪的模型。默认“只添加模型”，保留 Codex 当前站点和 Key；确实要换连接时，再选择“添加并切换站点 / Key”，下方会显示目标站点和密钥掩码。
5. 点击“预览并添加”。核对新增模型和连接变化，确认后写入配置；程序会先用本机 Codex 实际解析校验，再创建加密备份。
6. 完整退出并重新打开 Codex，在模型列表中选择新模型。

已有模型可以勾选以切换连接，不会重复追加。默认模型始终保留。更换 Key 后，原模型能否调用取决于新 Key 的分组权限。

“本次新发现”按同一来源上次成功刷新进行比较，不等于厂商刚发布。首次读取只建立记录；下次刷新重新计算标记。来源刷新失败时会保留比较记录，但不会将旧模型冒充本次成功结果。

## 参数来源与边界

- 现有 Codex 目录：原条目原样保留。
- Codex 内置目录：从本机实际可执行程序读取。
- 所选 Key 的 Sub2API Codex 目录：由站点返回，导入前通过真实解析器校验。
- 显式导入：在设置中导入包含 `models` 数组的完整 Codex JSON 目录，用于补充新模型参数。

工具不从其他模型猜测上下文和思考档位。新模型的名称可以先发现，完整参数缺失时需要等待站点适配或显式导入完整目录。四家官方目录是当前公布的候选集合；网页发生结构变化时可能需要调整 `sources.json`。刷新失败会明确提示，不以旧结果冒充本次成功。

站点需要兼容 Sub2API 的当前用户和密钥接口，以及页面登录存储约定。对站点自身的 OAuth、验证码页面显示正常，不代表其外部提供商一定允许嵌入式窗口；需要在目标站点验证。当前只管理用户级 `config.toml`：检测到启用中的 profile 覆盖待修改的目录或连接时会停止应用。启动参数指定的其他 profile、项目级配置或管理员设置不属于本工具管理范围。

本版本不发送推理测试请求，也不会创建 Key、修改分组、订阅或站点配置。参数校验通过与真实上游调用成功不是同一结论。

## 密钥与恢复

密码只在站点页面输入，工具不会记录密码。站点登录凭据只在本次运行中使用；界面、请求日志和配置预览不返回完整 API Key。

选中的 Key 由 Windows DPAPI 当前用户加密保存。Codex 配置使用官方的 `model_providers.<id>.auth.command`，自动调用 `runtime/ModelBridge.Key.exe` 读取密钥；这个内部程序无需手动打开，Key 不写入配置明文。加密备份只可由同一 Windows 用户解密。请保留完整工具目录及 `data`。本地加密不改变 HTTP 连接本身未加密的性质。

设置中的“恢复备份”可恢复本工具应用前的配置。恢复前会再次备份当前配置；之后需要重启 Codex。首次应用前若不存在配置文件，恢复后会留下等效的空配置文件。不要直接删除仍被配置引用的 `modelbridge` 模型目录或凭据文件。

## 源码运行与构建

使用 Python 3.12 创建项目虚拟环境，在项目内安装锁定依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
```

运行 `build.ps1`，产物默认放在源码目录的上一级。构建使用 Windows 已有的 .NET Framework C# 编译器生成凭据助手，使用 PyInstaller 生成桌面程序，不更改系统软件。

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
.\.venv\Scripts\python.exe .\main.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_core.py -v
```

开发浏览器模式：`main.py --serve --root <隔离数据目录>`，只用于页面验证；站点登录需要正常桌面模式。浏览器验证夹具为 `tests/ui_server.py`，它只修改 `source/work/ui-validation` 下的模拟配置。

业务需求和设计见 `_docs/模桥/00_总览与索引.md`。构建版本与检查结果见整体实现说明和月度迭代记录。

源码提交在本仓库，编译后的 Windows 下载包放在 [GitHub Releases](https://github.com/liuzhiqi3236/CodexModelBridge/releases)。发布包仅包含主程序、`runtime/ModelBridge.Key.exe`、默认 `sources.json` 和使用说明，不包含用户的 `data`、账号登录状态或 Codex 配置。
