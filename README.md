# astrbot_plugin_hextechmayhem

为 AstrBot 提供《英雄联盟》海克斯大乱斗英雄报告与海克斯强化查询，并将结果生成 1120px 深色主题图片。

插件是独立实现，不打包第三方英雄表、统计快照或攻略数据。运行时从 Riot Data Dragon、CommunityDragon、OP.GG、League of Legends Wiki、gtimg 和 Mayhempedia 获取所需信息；各来源相互隔离，单个来源故障不会阻断其他模块。

## 功能

- 按中文名、英文名、英雄内部 ID、数字 ID 或 gtimg 中文别名查询英雄。
- 英雄报告在一张图片中展示：
  - 英雄封面、简介、版本和 OP.GG 段位。
  - 棱彩、黄金、白银海克斯各最多 5 个。
  - 综合前三海克斯的 Wiki 机制中文翻译。
  - 最多 2 套召唤师技能、1 套技能加点顺序、2 套出门装、2 双鞋、3 套核心装。
  - 最多 3 套 Mayhempedia 社区六件套路线。
- 英雄报告不展示被动和 QWER 技能详情；“技能加点顺序”是 OP.GG 的独立统计模块。
- 中英文模糊搜索海克斯强化，展示 CommunityDragon 中文名称、稀有度、描述和可信图标。
- 选择一个 AstrBot LLM Provider 后，使用同一 Provider 识别英雄外号并忠实翻译 Wiki 英文机制说明。
- 所有第三方页面只按 HTML/JSON 文本解析，不执行脚本，不使用 `eval` 或 Node.js。
- 报告通过 Base64 图片发送，不在磁盘中保留报告或数据快照。

## 安装

将本目录复制到 AstrBot 插件目录：

```text
data/plugins/astrbot_plugin_hextechmayhem
```

也可以在 AstrBot WebUI 的插件安装页面填写仓库地址：

```text
https://github.com/muyikk/astrbot_plugin_hextechmayhem
```

AstrBot 会根据 `requirements.txt` 安装 `aiohttp`、`beautifulsoup4`、`certifi` 和 `Pillow`。安装完成后重载插件。

## 指令

根指令支持 `/hextech` 和 `/海克斯科技`，根指令、子指令与查询内容可以中英文混用。

| 英文指令 | 中文指令 | 功能 |
| --- | --- | --- |
| `/hextech help` | `/海克斯科技 帮助` | 显示帮助 |
| `/hextech hero <英雄>` | `/海克斯科技 海斗 <英雄>` | 生成完整英雄大乱斗图片报告 |
| `/hextech augment <关键词>` | `/海克斯科技 海克斯 <关键词>` | 生成海克斯搜索图片卡片 |
| `/hextech status` | `/海克斯科技 状态` | 查看各数据源与缓存状态 |

子指令别名：

- `hero`：`英雄`、`海斗`
- `augment`：`海克斯`、`强化`
- `help`：`帮助`
- `status`：`状态`

示例：

```text
/hextech hero 亚索
/海克斯科技 hero 暗裔剑魔
/hextech 英雄 Miss Fortune
/hextech hero 21
/hextech augment 珠光
/海克斯科技 海克斯 利刃华尔兹
/hextech 状态
```

当前 AstrBot 支持 `GreedyStr` 时可直接输入多词英文名；插件也会从原始消息恢复完整参数，以兼容较早的 AstrBot 4.x。

## 数据来源与展示规则

### 英雄基础资料

英雄列表、中文名、标题、简介、封面和图标来自 Riot Data Dragon。查询会匹配中文名、中文称号、英文内部 ID、数字 ID，以及忽略空格、标点后的英文名。

开启 `enable_gtimg_fallback` 后，本地 Riot 名称未匹配时会尝试 gtimg 别名；Riot 或 CommunityDragon 图片失败时也会尝试可信的 gtimg 英雄或装备图片。gtimg 不提供统计推荐。

### OP.GG 统计

插件只读取 OP.GG `zh-cn` 海克斯大乱斗服务端渲染页面，不调用私有接口。海克斯图片名称必须再次匹配 CommunityDragon 后才能进入报告。胜率、选择率、场次等数字只有在页面带明确字段标签时才展示，不推算缺失值。

OP.GG 页面结构变化、限流或访问验证只会使 OP.GG 模块显示暂不可用；Riot 英雄资料、海克斯搜索和社区路线仍可继续工作。

### 海克斯资料与 Wiki 机制

CommunityDragon 是海克斯主数据源，提供稳定 ID、API 名、中文名称、稀有度、描述和图标。`/hextech augment` 同时匹配中文名、英文名、API 名和 ID。

开启 `enable_wiki_enrichment` 后，插件从官方 League of Legends Wiki 读取公开机制说明：

- 英雄报告只处理 OP.GG 综合排序前三的海克斯。
- 海克斯搜索只处理当前展示的结果。
- Wiki 原文只交给 LLM 做忠实翻译；模型必须返回严格 JSON。
- 未选择 Provider、返回格式错误或翻译失败时，省略机制补充，继续显示 CommunityDragon 中文描述。

### Mayhempedia 社区路线

完整六件套通过 jsDelivr 读取 Mayhempedia GitHub 仓库中的公开 MIT 路线文件，报告中始终标注为“社区攻略”，不与 OP.GG 统计混称。插件验证英雄数字 ID，并且只接受确实包含至少六件装备的路线；最多显示三条。实际只有 0–2 条时只展示真实存在的数据，不使用核心装与热门单件推导补齐。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | ---: | --- |
| `llm_provider_id` | 空 | 同时用于英雄别名识别和 Wiki 翻译；选择 Provider 即启用，留空则关闭全部 LLM 功能 |
| `enable_opgg_source` | `true` | 启用 OP.GG 英雄统计模块 |
| `enable_wiki_enrichment` | `true` | 启用 Wiki 机制补充和按需中文翻译 |
| `enable_gtimg_fallback` | `true` | 启用 gtimg 别名与图片降级 |
| `enable_mayhempedia_source` | `true` | 启用 Mayhempedia 社区六件套 |
| `request_timeout_seconds` | `20` | 单次上游请求总超时，范围 5–120 秒 |
| `cache_ttl_seconds` | `3600` | 所有实时数据内存缓存有效期，范围 60–86400 秒 |
| `max_concurrent_requests` | `3` | 最大并发上游请求数，范围 1–10 |
| `max_results` | `5` | 海克斯搜索最多展示数，范围 1–10 |
| `max_augments_per_rarity` | `5` | 英雄报告每个稀有度最多展示数，范围 1–10 |
| `proxy` | 空 | 可选 HTTP 代理，例如 `http://127.0.0.1:7890` |

旧配置 `max_interactions` 已弃用。若配置文件中仍存在该字段，插件记录一次提示并使用 `max_augments_per_rarity` 的默认值，不再将旧值解释为新的分档限制。

## 缓存、状态与降级

所有英雄、海克斯、页面、路线、翻译和图片缓存都只存在于 AstrBot 当前进程内。缓存过期后：

1. 对对应来源发起一次单飞刷新；并发查询复用同一刷新结果。
2. 刷新成功则替换该来源缓存。
3. 刷新失败但存在旧缓存时继续使用，并在图片和 `/hextech status` 中标记旧缓存。
4. 冷启动失败时只隐藏该来源模块；Data Dragon 英雄基础资料不可用时才终止英雄查询。

`/hextech status` 分别展示 Data Dragon、CommunityDragon、OP.GG、League Wiki、gtimg 和 Mayhempedia 的启用、正常、旧缓存、失败或未加载状态。

图片下载仅允许 HTTPS 的 Riot CDN、`*.riotcdn.net`、CommunityDragon、OP.GG 静态 CDN 和 gtimg 域名，并验证最终重定向域名、Content-Type 和响应体大小。单个图标失败时使用占位块；HTML 渲染失败时发送包含相同数据字段的文字报告。

## 开发与测试

默认测试完全离线：

```bash
cd /Users/feewee009/myCode
python3 -m unittest discover -s astrbot_plugin_hextechmayhem/tests -v
python3 -m py_compile astrbot_plugin_hextechmayhem/*.py
```

测试覆盖中英文英雄匹配、gtimg 别名、缓存单飞与旧数据降级、CommunityDragon 合并、OP.GG 分区与数量限制、访问验证识别、Wiki 机制匹配、Mayhempedia 六件套校验、可信图片域名、HTML 转义、单图模块和配置项。

## 声明

- 英雄资料与资产：[Riot Games Data Dragon](https://developer.riotgames.com/docs/lol)
- 海克斯定义与资产：[CommunityDragon](https://www.communitydragon.org/)
- 英雄统计：[OP.GG](https://op.gg/zh-cn/lol/modes/aram-mayhem)
- 机制说明：[League of Legends Wiki](https://wiki.leagueoflegends.com/en-us/ARAM%3A_Mayhem/Augments)
- 中文别名及图片降级：[腾讯英雄联盟公开静态资源](https://game.gtimg.cn/)
- 社区攻略路线：[Mayhempedia](https://github.com/boxsbraindump/Mayhempedia)
- 功能设计参考：[PayneXie/astrbot_plugin_hextech](https://github.com/PayneXie/astrbot_plugin_hextech)

本插件与 Riot Games、OP.GG、League of Legends Wiki、腾讯游戏和 Mayhempedia 均无官方关联。第三方站点结构、服务条款或数据格式变化可能导致部分模块暂时不可用。请遵守相关服务条款和 Riot Games 的知识产权政策。

项目采用 MIT License，详细说明见 `LICENSE` 与 `NOTICE`。
