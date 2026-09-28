# astrbot_plugin_hextechmayhem

为 AstrBot 提供《英雄联盟》海克斯大乱斗英雄报告与海克斯强化查询，并将结果生成主题图片。

插件是独立实现，不打包第三方英雄表、统计快照或攻略数据。Riot Data Dragon 提供英雄列表与高清封面；ARAMGG 提供英雄名称、别名、海克斯、统计、装备、技能和图片；LLM 仅作为玩家黑话的最终识别兜底。OP.GG 与 Mayhempedia 保留为可选来源。

## 数据来源

- 英雄资料与资产：[Riot Games Data Dragon](https://developer.riotgames.com/docs/lol)
- 国内优先英雄统计：[ARAMGG 数据 API](https://data.dtodo.cn/api/v1/zh-CN/docs/cf-data-api.md)
- 备用英雄统计：[OP.GG](https://op.gg/zh-cn/lol/modes/aram-mayhem)
- OP.GG 模式海克斯定义与资产：[CommunityDragon](https://www.communitydragon.org/)
- 社区攻略路线：[Mayhempedia](https://github.com/boxsbraindump/Mayhempedia)
- 功能设计参考：[PayneXie/astrbot_plugin_hextech](https://github.com/PayneXie/astrbot_plugin_hextech)  （主要是作者没有维护了）

## 功能

- 按中文名、英文名、英雄内部 ID、数字 ID 或 ARAMGG 别名查询英雄。
- 查询所选信息源的英雄综合强度排行榜；统计字段仅在来源真实提供时展示。
- 英雄报告在一张图片中展示：
  - 英雄封面、海斗数据版本和强度分档。
  - 棱彩、黄金、白银海克斯各最多 5 个。
  - 最多 2 套召唤师技能、1 套技能加点顺序、2 套出门装、2 双鞋、3 套核心装。
  - ARAMGG 模式额外展示最多 4 个真实流派、每个流派最多 3 套完整技能序列和最多 6 组情境装备。
  - ARAMGG 模式额外展示最多 5 套达到样本门槛的三海克斯组合、6 件热门单件表现、数据来源说明和 3 篇相关文章标题。
  - 最多 3 套 Mayhempedia 社区六件套路线。
- 英雄报告不展示被动和 QWER 技能详情；“技能加点顺序”来自所选海斗信息源。
- 中英文模糊搜索海克斯强化，展示 ARAMGG 中文名称、稀有度、描述和可信图标。
- 选择一个 AstrBot LLM Provider 后，用于识别英雄外号。
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

插件仅注册三个独立中文指令，不再注册 `/hextech`、`/海克斯科技` 或其他复合二级指令。

| 指令 | 功能 |
| --- | --- |
| `/海斗 <英雄名>` | 生成英雄海克斯大乱斗图片报告 |
| `/海克斯 <海克斯名>` | 生成海克斯搜索图片卡片 |
| `/海斗排名` | 生成英雄综合强度前 10 名图片卡片 |

示例：

```text
/海斗 亚索
/海斗 Miss Fortune
/海斗 21
/海克斯 珠光护手
/海斗排名
```

当前 AstrBot 支持 `GreedyStr` 时可直接输入多词英文名；插件也会从原始消息恢复完整参数，以兼容较早的 AstrBot 4.x。

`/海斗排名`生成按强度档位分组的图片卡片，展示英雄头像、定位、综合排名、补丁、统计日期、来源和区域。它遵循所选信息源自己的综合排名，不自行按单一胜率重排：ARAMGG 模式直接使用官方推荐入口 `/api/v1/zh-CN/champions.json` 的榜单顺序；OP.GG 模式解析其公开服务端页面中的英雄排名与强度档位，并且不会请求 ARAMGG。胜率、登场率、场次和排名变化仅在当前响应真实提供时展示；缺失字段不会显示或补成 `0`，也不会为排名变化额外访问原始兼容接口。

## 数据来源与展示规则

### 英雄基础资料

ARAMGG 模式的英雄列表、中文名、称号、英文别名和图标来自 ARAMGG；OP.GG 模式改由 Riot Data Dragon 提供英雄列表和图标。两种模式都会匹配中文名、中文称号、英文内部 ID、数字 ID，以及忽略空格、标点后的英文名。报告仅展示英雄封面，不再读取或展示英雄简介。

当前信息源的本地名称和别名仍无法匹配时，插件才调用所选 LLM 识别玩家黑话；模型结果必须重新匹配当前英雄列表。

### ARAMGG 与 OP.GG 统计

默认使用 ARAMGG 单英雄详情 API。一次请求即可取得推荐海克斯、胜率、登场率、场次、召唤师技能、技能加点、出门装和核心装备；首次查询每个英雄消耗 2 credits，缓存有效期内不会重复请求。API Key 仅通过鉴权请求头发送，不写入报告或日志。

同一次单英雄响应还会解析多流派 Build、完整 18 级技能序列、情境装备、三海克斯组合、热门单件、统计来源/区域/日期以及相关文章标题，不产生额外 API 请求。只有接口真实返回的模块与统计字段才会展示；`fullItems` 为空时不会从核心装推导六神装。

选择 `opgg` 时，插件读取 OP.GG `zh-cn` 海克斯大乱斗服务端渲染页面，不调用私有接口。海克斯名称必须再次匹配 CommunityDragon 海克斯目录后才能进入报告。胜率、登场率、场次等数字只有在来源提供明确字段时才展示，不推算缺失值，也不把 `null` 转换成 `0`。

OP.GG 页面结构变化、限流或访问验证只会使 OP.GG 模块显示暂不可用；Riot 英雄资料、海克斯搜索和社区路线仍可继续工作。

### 海克斯资料

ARAMGG 模式由 ARAMGG 提供海克斯目录；OP.GG 模式改由 CommunityDragon 提供稳定 ID、API 名、中文/英文名称、稀有度、描述和图标。`/海克斯` 使用当前模式的目录进行匹配。

- 英雄报告按照所选信息源的官方排序展示海克斯。
- 海克斯搜索只处理当前展示的结果。

### Mayhempedia 社区路线

完整六件套通过 jsDelivr 读取 Mayhempedia GitHub 仓库中的公开 MIT 路线文件，报告中始终标注为“社区攻略”，不与 OP.GG 统计混称。插件验证英雄数字 ID，并且只接受确实包含至少六件装备的路线；最多显示三条。实际只有 0–2 条时只展示真实存在的数据，不使用核心装与热门单件推导补齐。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | ---: | --- |
| `llm_provider_id` | 空 | 用于英雄别名识别的 LLM Provider。仅在 ARAMGG 英雄名称和别名无法匹配时识别玩家黑话；留空即关闭 LLM 兜底。模型结果只会重新匹配 ARAMGG 英雄列表，不会直接作为网络地址使用，例如“卢仙／奥巴马”对应“圣枪游侠”、“奶妈”对应“众星之子” |
| `mayhem_source` | `aramgg` | 选择海斗信息源，可选 `aramgg` 或 `opgg`。用于英雄胜率、登场率、场次、海克斯和出装统计；国内优先使用 ARAMGG，OP.GG 可能访问较慢 |
| `aramgg_api_key` | 空 | ARAMGG 数据 API Key，仅在 `mayhem_source=aramgg` 时使用。前往 [ARAMGG 开发者后台](https://data.dtodo.cn/developer.html)，使用 GitHub 登录并创建 API Key，然后填写完整的 `hx_live_...` 密钥。密钥只作为鉴权请求头发送，不会写入报告或日志。默认每天 200 credits，单个英雄首次查询消耗 2 credits，缓存命中不会重复消耗；选择 `opgg` 时可留空 |
| `enable_mayhempedia_source` | `false` | 是否启用 Mayhempedia 社区六件套路线。最多展示三条真实社区攻略路线，数据不足时不会使用插件推导组合补齐；开启后可能增加查询耗时 |
| `request_timeout_seconds` | `20` | 访问 Riot Data Dragon、ARAMGG、OP.GG 和 Mayhempedia 的请求超时，单位为秒；有效范围 5–120 |
| `cache_ttl_seconds` | `3600` | 实时数据的内存缓存有效时间，单位为秒。刷新失败时继续使用进程内最近一次成功的数据；插件重启后内存缓存消失 |
| `max_concurrent_requests` | `3` | 最大并发上游请求数；有效范围 1–10，设置过高可能触发第三方站点限流 |
| `max_results` | `5` | `/海克斯`搜索最多展示的结果数量；有效范围 1–10 |
| `max_augments_per_rarity` | `5` | 英雄报告中每个稀有度最多展示的海克斯数量；有效范围 1–10，分别限制棱彩、黄金和白银推荐 |
| `proxy` | 空 | 访问实时数据源时使用的可选 HTTP 代理，例如 `http://127.0.0.1:7890`；留空时不显式设置代理 |

旧配置 `max_interactions` 已弃用。若配置文件中仍存在该字段，插件记录一次提示并使用 `max_augments_per_rarity` 的默认值，不再将旧值解释为新的分档限制。

## 缓存、状态与降级

ARAMGG 英雄列表、英雄别名和海克斯目录会持久化到：

```text
data/plugin_data/astrbot_plugin_hextechmayhem/cache/
├── aramgg_champions.json
└── aramgg_augments.json
```

这些文件仅供 `mayhem_source=aramgg` 使用。选择 `opgg` 时，英雄列表来自 Riot Data Dragon，海克斯目录来自 CommunityDragon，统计与出装来自 OP.GG；插件不会读取 ARAMGG 目录缓存，也不会向 `data.dtodo.cn` 发起 API、`config.json` 或图片请求，因此不需要配置 ARAMGG API Key，也不会消耗 ARAMGG credits。

插件启动后先读取本地文件，再访问不消耗 credits 的 `config.json` 检查 `dataVersion`。版本一致时直接使用本地数据；版本变化时才下载并原子替换缓存文件。网络不可用但本地文件有效时继续使用本地数据。

单英雄统计、OP.GG 页面、社区路线、LLM 结果和图片仍只保存在当前进程内。缓存过期后：

1. 对对应来源发起一次单飞刷新；并发查询复用同一刷新结果。
2. 刷新成功则替换该来源缓存。
3. 刷新失败但存在旧缓存时继续使用，并在图片或排行榜文字中标记缓存数据。
4. 冷启动失败时只隐藏该来源模块；Data Dragon 英雄基础资料不可用时才终止英雄查询。

图片下载仅允许 HTTPS 的 Riot CDN、CommunityDragon、ARAMGG CDN 和 OP.GG 静态 CDN，并验证最终重定向域名、Content-Type 和响应体大小。单个图标失败时使用占位块；HTML 渲染失败时发送包含相同数据字段的文字报告。

## 声明

本插件与 Riot Games、ARAMGG、OP.GG、腾讯游戏和 Mayhempedia 均无官方关联。第三方站点结构、服务条款或数据格式变化可能导致部分模块暂时不可用。请遵守相关服务条款和 Riot Games 的知识产权政策。

项目采用 MIT License，详细说明见 `LICENSE` 与 `NOTICE`。
