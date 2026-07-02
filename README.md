# phimg

<p align="center">
  <img src="https://derpicdn.net/img/download/2012/7/7/36211__safe_artist-colon-charleston-
  dash-and-dash-itchy_philomena_phoenix_g4_arizona_pet_phoenix+arizona.png" width="128" alt="Philomena" />
</p>

让机器人通过 [Philomena API](https://github.com/philomena-dev/philomena) 在使用 Philomena 搭建的图站（例如 [Derpibooru](https://derpibooru.org)）上使用标签（tags）搜图。

从 [koishi-plugin-phimg](https://github.com/JessDaodao/koishi-plugin-phimg) 移植为 AstrBot 插件。

## 功能

- 通过标签搜索图片
- 引用图片或随消息附图时以图搜图（默认相似度距离 0.25）
- 群聊粒度的搜图配置（开关、自定义标签、全局标签）
- 支持在插件配置中填写网络代理
- 提供 LLM 工具 `phimg_search`，用户用自然语言要图时由模型自动调用

## 配置

在 AstrBot 管理面板的插件配置页可以对以下项目进行配置：

| 配置项 | 说明 |
| --- | --- |
| `apiKey` | Philomena API 密钥，可留空 |
| `apiUrl` | 图站域名，例如 `derpibooru.org` |
| `filterId` | 搜索使用的 Filter ID，例如 `100073` |
| `defaultTags` | 全局默认标签，例如 `["safe"]` |
| `enabledByDefault` | 新群聊是否默认启用搜图 |
| `useGlobalTagsByDefault` | 新群聊是否默认启用全局标签 |
| `groupOnly` | 是否仅限在群聊中使用，关闭后私聊也可使用（默认开启） |
| `proxy` | 网络代理地址，例如 `http://127.0.0.1:7890`，留空则不使用代理 |
| `timeout` | 请求超时时间（秒） |

## 指令

- `搜图 [tags|distance]` — 通过标签搜索图片；引用图片或随消息附图时会以图搜图（默认距离 0.25）
- `搜图-c [选项]` — 群内搜图配置（管理员可用）

详细参数请在群内输入 `搜图` 或 `搜图-c` 查看。

## License

AGPL-3.0-only

## Logo

图标来源：[Trixiebooru](https://trixiebooru.org/images/36211)
