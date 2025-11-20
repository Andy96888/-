
🤖 Telegram 代收付记账机器人 (Bookkeeping Bot)
这是一个基于 Python 的 Telegram 群组记账机器人，专为代收付、财务记录等场景设计。它采用异步架构 (asyncio) 和 SQLite 数据库，具有数据持久化、多群组隔离、权限管理以及针对网络波动的自动重试机制。

✨ 核心功能
🛡️ 高可靠性：内置网络请求重试机制 (_robust_telegram_call)，在 API 超时或网络波动时自动重试，防止漏单。

📊 完整账务周期：支持“上课”（开账）和“下课”（结账）流程，自动生成统计报表。

💰 实时记账：支持快速录入入款（+）和下发（-），自动计算净额和结余。

👥 权限管理：

管理员：拥有所有权限，可管理操作员。

操作员：可进行日常记账操作。

普通群员无法干扰账目。

📂 数据隔离：每个群组拥有独立的 SQLite 数据库文件 (.db)，互不干扰。

📝 详细日志：按天轮转的日志系统，方便排查问题。

🔙 容错操作：支持撤销上一笔错误账单，支持录入上期结余。

🛠️ 环境要求
Python 3.8+

Telegram Bot Token

🚀 快速开始
1. 克隆项目
Bash

git clone https://github.com/your-username/your-repo-name.git
cd your-repo-name
2. 安装依赖
本项目依赖 python-telegram-bot 和 aiosqlite。

Bash

pip install python-telegram-bot aiosqlite
3. 配置 Token
推荐方式：设置环境变量（Linux/Mac）

Bash

export BOT_TOKEN="你的_TELEGRAM_BOT_TOKEN"
或者在 Windows PowerShell 中：

PowerShell

$env:BOT_TOKEN="你的_TELEGRAM_BOT_TOKEN"
(如果不设置环境变量，请修改代码中的默认 Token，但严禁提交到公开仓库)

4. 运行机器人
Bash

python dsf.py
运行成功后，会在目录下自动生成 logs/ 和 data/ 文件夹。

📖 指令使用指南
本机器人仅在 群组 (Group/Supergroup) 中工作。
⚡️ 核心流程指令描述示例上课开启一个新的记账周期（如果存在上期结余会提示导入）。
上课下课结束当前周期，生成最终统计报表并归档。
下课+金额记录入款（支持备注）。
+1000 或 +500 张三入款-金额记录下发/支出（支持备注）。
-500 或 -500 打款李四
🔧 修正与管理指令描述示例结余录入初始金额或手动调整结余。
结余 +2000撤销删除最后一条记账记录（防手误）。
撤销
帮助显示操作帮助菜单。帮助
👮 人员权限（仅管理员可用）指令描述方式设置操作员将某用户设为记账员。回复该用户消息发送 设置操作员 或发送 设置操作员 @username删除操作员移除某用户的记账权限。回复该用户消息发送 删除操作员 或发送 删除操作员 @username当前操作员查看当前群组有权限的记账员列表。当前操作员
📂 项目结构
Plaintext.
├── dsf.py              # 主程序源码
├── data/               # [自动生成] 存放各群组的 SQLite 数据库 (.db)
├── logs/               # [自动生成] 存放运行日志
└── README.md           # 说明文件
⚙️ 技术细节数据库：使用 aiosqlite 进行异步数据库操作，保证高并发下的性能。表结构：cycles: 记录记账周期（上课/下课时间）。bills: 存储具体的流水账单。users & operators: 用户信息与权限表。previous_balances: 存储跨周期的结余数据。网络优化：代码中实现了 _robust_telegram_call 装饰器逻辑，针对 TimedOut 错误进行指数退避重试，大幅提高了在国内或不稳定网络环境下的连接成功率。
⚠️ 免责声明本机器人仅供技术研究与个人记账使用。使用者需自行承担数据安全与使用的相关责任。请勿将本程序用于任何非法用途。
