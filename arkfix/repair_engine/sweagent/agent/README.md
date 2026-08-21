# Agent 模块

`agent` 目录包含模型推理逻辑，以及模型与 `SWEEnv` 交互的封装。

下面按文件说明主要职责。

#### `agents.py`

定义 `Agent` 类，负责 agent 与环境之间的交互。`AgentConfig` 和 `AgentArguments` 数据类把运行参数汇总成统一配置。

- `Agent`：处理模型行为和环境交互的主类。
- `__init__`：初始化模型、assistant、配置和参数。
- `state_command`：获取用于抽取环境状态的 bash 命令。
- `setup`：重置 cost 统计，初始化 system message 和 demonstration，并返回需要在环境中定义的一组 bash 命令。
- `forward`：模型推理主入口。
- `forward_model`：选择合适的 observation template，然后调用模型推理。
- `forward_with_format_check`：调用 `forward_model`，并通过重试处理被拦截或格式错误的 action。
- `forward_with_error_check`：在 `forward_with_format_check` 外层增加异常处理。

#### `commands.py`

定义自定义命令抽象。这些命令不是原生命令，而是在 `swe-agent` 环境中通过 bash 实现、供 agent 调用的函数。该文件还提供从 `.sh` 文件抽取命令文档、把脚本编译成独立 `Command` 对象的 helper，并描述每个 action 的输入、输出和控制流模板。

- `AssistantMetadata`：定义格式化 sub-assistant 调用输入/输出的模板。
- `Command`：定义自定义命令字段。
- `ControlMetadata`：定义用于格式化下一次 `forward` observation 的模板字段，目前仍是 WIP。
- `generate_command_docs`：从每个命令抽取 docstring，生成完整命令文档。
- `parse_command_file`：把 bash 文件内容转换成独立的 `Command` 对象。

#### `models.py`

定义 API 模型推理抽象。`BaseModel` 同时包含 cost 相关字段，用于追踪单次模型运行中的实例级和总费用。

- `AnthropicModel`：处理 Anthropic 模型推理和 cost logging。
- `BedrockModel`：处理 Amazon Bedrock 提供的模型推理和 cost logging，目前只覆盖 Anthropic Claude。
- `APIStats`：每次模型推理后更新的 cost 统计字段。
- `BaseModel`：定义更新 cost 统计的公共逻辑。
- `get_model`：根据参数和命令初始化 `[Anthropic|Bedrock|Human|OpenAI]Model`。
- `HumanModel`：处理人工 worker。
- `ModelArguments`：模型名、超参数和 cost limit 参数。
- `OpenAIModel`：处理 OpenAI 模型推理和 cost logging。

#### `parsing.py`

定义模型输出解析抽象。`Parsing` 类用于从模型输出中提取关键信息，并格式化成 `Agent` 可消费的响应。

- `Parsing`：定义解析模型输出的公共逻辑。

#### `history_processors.py`

定义环境历史处理抽象。`HistoryProcessor` 类用于从环境历史中提取信息，并格式化成 `Agent` 可消费的响应。

- `HistoryProcessor`：定义处理环境历史的公共逻辑。
- `DefaultHistoryProcessor`：默认实现。

### 环境用法

- 跳过一个任务实例：使用 `skip` 关键字。
- 提交评测：使用 `submit` 关键字。
- 退出 `SWEEnv` 环境：发送键盘中断，也就是 `^c`。
