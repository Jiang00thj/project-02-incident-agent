# 基准结果溯源

DeepSeek 真模型（deepseek-flash，temp=0，BENCH_API_KEY 只走环境变量，**本目录不含任何密钥**）。

| 臂 | 变量 | 文件 | 任务成功 |
|---|---|---|---|
| A 原始提示词 | scan_logs 提示词无 keyword 匹配语义 | `deepseek_round{1,2,3}.json` | INC-2401 0/3，总体 2/9 |
| B 只改 MCP 工具描述 | query_logs description 加子串匹配说明 | `deepseek_descv2_round{1,2,3}.json` | 与 A 完全一致（模型看不到工具描述） |
| C 把匹配语义写进节点提示词 | _system_prompt("scan_logs") | `deepseek_promptv2_round{1,2,3}.json` | INC-2401 修复 2/3，总体 8/9 |

复现：`BENCH_API_KEY=<你的key> python bench.py --model deepseek --json results/xxx.json`

结论：接口描述写在哪一层（工具 schema vs 节点提示词）决定了"改描述"是否生效——自研编排里 LLM 决策走提示词时，工具描述是模型看不见的死代码。项目一"瓶颈在描述不在模型"的结论在第三种语境复现。
