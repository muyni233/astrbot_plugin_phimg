"""phimg - 通过 Philomena API 在使用 Philomena 搭建的图站上用 tags 搜图。

从 koishi-plugin-phimg 移植而来。群聊粒度的配置持久化在 AstrBot 的 data 目录下，
所有网络请求通过 aiohttp 异步发起，并支持在插件配置中填写网络代理。
"""

import asyncio
import json
import random
import re
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import At, Image, Plain, Reply, Video
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

# 全角标点 -> 半角标点，方便用户用中文输入法输入搜索语法
TRANSLATION_TABLE: dict[str, str] = {
    "；": ";",
    "：": ":",
    "，": ",",
    "（": "(",
    "）": ")",
    "【": "[",
    "】": "]",
    "《": "<",
    "》": ">",
    "？": "?",
    "！": "!",
    "。": ".",
    "、": ",",
}

VIDEO_TYPES = {"webm", "mp4"}

# 需要跟随一个取值的选项（取下一个空格分隔的 token 作为值）
VALUE_OPTIONS = {"--pp", "--p", "--sf", "--sd", "--i", "--add", "--rm"}
# 仅作为标志的选项（无取值）
FLAG_OPTIONS = {"--tags", "--status", "--on", "--off", "--onglobal", "--offglobal"}

SEARCH_HELP = """用法: /搜图 [tags|distance]

用法说明:
  引用图片: 进行以图搜图 (默认距离 0.25)
  直接发图: 发送指令时附带图片进行以图搜图
  输入文本: 进行标签搜索

可选项:
  --tags             获取当前群聊内置标签列表
  --status           获取当前群聊的搜图功能状态
  --pp [num]         每页数量 (默认50)
  --p [num]          页码 (默认1)
  --sf [field]       排序字段 (默认score)
  --sd [desc|asc]    排序方向 (默认desc)
  --i [index]        选择结果索引 (默认随机)
—————
Powered by
Phimg for AstrBot"""

CONFIG_HELP = """用法: /搜图-c [选项]

可选项:
  --add [tags]       添加标签
  --rm [tags]        删除标签
  --on               开启搜图
  --off              关闭搜图
  --onglobal         启用全局标签
  --offglobal        关闭全局标签
—————
Powered by
Phimg for AstrBot"""


class PhimgError(Exception):
    """搜图过程中产生的、需要直接回复给用户的异常。"""


def translate_text(text: str) -> str:
    """将全角标点转换为半角标点。"""
    if not text:
        return text
    return "".join(TRANSLATION_TABLE.get(ch, ch) for ch in text)


def parse_options(text: str) -> tuple[dict[str, Any], str]:
    """从一段文本中解析出 ``--flag`` / ``--flag value`` 形式的选项。

    返回 (选项字典, 剩余的标签文本)。选项字典的 key 为选项名（含 ``--``），
    标志型选项的 value 为 ``True``，取值型选项的 value 为对应的字符串。
    """
    tokens = text.split()
    options: dict[str, Any] = {}
    tag_tokens: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in VALUE_OPTIONS and i + 1 < len(tokens):
            options[token] = tokens[i + 1]
            i += 2
        elif token in VALUE_OPTIONS:
            # 选项出现在末尾、没有取值，忽略该选项
            i += 1
        elif token in FLAG_OPTIONS:
            options[token] = True
            i += 1
        else:
            tag_tokens.append(token)
            i += 1
    return options, " ".join(tag_tokens)


class PhimgPlugin(Star):
    """通过 Philomena API 搜图的 AstrBot 插件。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        # 持久化数据存放在 data/plugin_data/<插件名> 下，避免更新/重装时被覆盖
        self.data_dir: Path = StarTools.get_data_dir()
        self.config_path: Path = self.data_dir / "group_configs.json"
        self._group_configs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        # 记录已发起“关闭全局标签”二次确认的用户，key 为 "scope-user_id"
        self._confirm_off_global: set[str] = set()
        self._load_configs()

    async def initialize(self) -> None:
        """插件加载时创建复用的 HTTP 会话。"""
        timeout = aiohttp.ClientTimeout(total=float(self.config.get("timeout", 30.0)))
        self._session = aiohttp.ClientSession(timeout=timeout)

    async def terminate(self) -> None:
        """插件卸载/重载时关闭 HTTP 会话。"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # 群聊配置的持久化
    # ------------------------------------------------------------------

    def _load_configs(self) -> None:
        """从磁盘加载群聊配置。文件不存在时使用空配置。"""
        try:
            if self.config_path.exists():
                with open(self.config_path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._group_configs = data
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"phimg: 读取群聊配置失败，将使用空配置: {e}")
            self._group_configs = {}

    def _save_configs(self) -> None:
        """将群聊配置写入磁盘。"""
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self._group_configs, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning(f"phimg: 写入群聊配置失败: {e}")

    async def get_group_config(self, scope_id: str) -> dict[str, Any]:
        """获取指定作用域（群聊或私聊会话）的配置，不存在时按默认值创建一份。"""
        async with self._lock:
            if scope_id not in self._group_configs:
                self._group_configs[scope_id] = {
                    "enabled": bool(self.config.get("enabledByDefault", True)),
                    "useGlobalTags": bool(
                        self.config.get("useGlobalTagsByDefault", True)
                    ),
                    "customTags": [],
                }
                self._save_configs()
            # 返回一份拷贝，避免调用方意外修改内存中的数据
            return dict(self._group_configs[scope_id])

    async def update_group_config(
        self,
        scope_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """更新指定作用域的配置并落盘，返回更新后的配置拷贝。"""
        async with self._lock:
            if scope_id not in self._group_configs:
                self._group_configs[scope_id] = {
                    "enabled": bool(self.config.get("enabledByDefault", True)),
                    "useGlobalTags": bool(
                        self.config.get("useGlobalTagsByDefault", True)
                    ),
                    "customTags": [],
                }
            self._group_configs[scope_id].update(data)
            self._save_configs()
            return dict(self._group_configs[scope_id])

    # ------------------------------------------------------------------
    # 与 Philomena API 交互
    # ------------------------------------------------------------------

    def _resolve_scope(self, event: AstrMessageEvent) -> str | None:
        """返回配置作用域 ID。

        群聊返回 group_id；私聊在 ``groupOnly`` 关闭时返回 unified_msg_origin
        （按私聊会话独立保存配置），开启时返回 None 表示不允许使用。
        """
        group_id = event.get_group_id()
        if group_id:
            return group_id
        if self.config.get("groupOnly", True):
            return None
        return event.unified_msg_origin

    def _host(self) -> str:
        """从配置的图站域名中提取 host（去掉协议和路径）。"""
        url = str(self.config.get("apiUrl", "derpibooru.org"))
        host = re.sub(r"^https?://", "", url).split("/")[0]
        return host or "derpibooru.org"

    def _proxy(self) -> str | None:
        proxy = str(self.config.get("proxy", "") or "").strip()
        return proxy or None

    async def make_request(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """调用 Philomena 搜索接口。

        method 为 ``images`` 时发起 GET 标签搜索；为 ``reverse`` 时发起 POST 以图搜图。
        """
        if self._session is None:
            raise PhimgError("插件尚未初始化完成，请稍后再试。")

        endpoint = f"https://{self._host()}/api/v1/json/search/{method}"
        query: dict[str, Any] = {"filter_id": self.config.get("filterId", 100073)}
        # API Key 放在 query string 中
        key = params.pop("key", None)
        if key:
            query["key"] = key

        headers = {"Accept": "application/json"}
        proxy = self._proxy()

        # 发起请求并读取原始响应
        try:
            if method == "images":
                query.update({k: v for k, v in params.items() if v is not None})
                ctx = self._session.get(
                    endpoint,
                    params=query,
                    headers=headers,
                    proxy=proxy,
                )
            else:
                # 以图搜图：表单字段以 application/x-www-form-urlencoded 发送
                form = {k: str(v) for k, v in params.items() if v is not None}
                ctx = self._session.post(
                    endpoint,
                    params=query,
                    data=form,
                    headers=headers,
                    proxy=proxy,
                )
            async with ctx as resp:
                status = resp.status
                text = await resp.text()
        except aiohttp.ClientError as e:
            logger.warning(f"phimg API 网络错误: {method} {e}")
            raise PhimgError("API 请求失败（网络错误），详情见日志") from e
        except Exception as e:  # noqa: BLE001 - 兜底，确保异常以用户可读的形式返回
            logger.warning(f"phimg API 请求异常: {method} {e}")
            raise PhimgError("API 请求失败，详情见日志") from e

        # 解析响应体
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None

        # 非 200 或非 JSON 视为请求失败：记录状态码与响应体片段，便于排查
        # （常见原因：Cloudflare 拦截、代理返回错误页、域名错误等）
        if status != 200 or not isinstance(data, dict):
            snippet = " ".join(text.strip().split())[:200]
            logger.warning(
                f"phimg API 请求失败: {method} HTTP {status} body={snippet!r}"
            )
            if status == 404:
                raise PhimgError("未找到匹配的图片")
            if not isinstance(data, dict):
                raise PhimgError("API 返回了非 JSON 数据，详情见日志")
            raise PhimgError(f"API 请求失败 (HTTP {status})")

        images = data.get("images")
        if not images:
            # 正常的无结果情况，不打警告日志
            raise PhimgError("未找到匹配的图片")
        return data

    # ------------------------------------------------------------------
    # 结果构造辅助
    # ------------------------------------------------------------------

    def build_media(self, selected: dict[str, Any]) -> Image | Video | Plain:
        """根据单条搜索结果构造对应的消息组件（图片 / 视频）。"""
        reps = selected.get("representations")
        if not reps:
            return Plain("图片数据解析错误")
        file = str(reps.get("full", ""))
        # webm 使用 medium 尺寸，其余使用 large 尺寸
        url = (
            reps.get("medium", "") if file.endswith(".webm") else reps.get("large", "")
        )
        file_type = file.rsplit(".", 1)[-1].lower() if "." in file else ""
        if file_type in VIDEO_TYPES:
            return Video.fromURL(url)
        return Image.fromURL(url)

    def extract_image_url(self, event: AstrMessageEvent) -> str | None:
        """提取用于以图搜图的图片 URL。

        优先取引用消息中的图片，其次取当前消息附带的图片。
        """
        messages = event.get_messages()
        # 1. 引用消息中的图片
        for comp in messages:
            if isinstance(comp, Reply):
                for sub in comp.chain or []:
                    if isinstance(sub, Image):
                        url = sub.url or sub.file
                        if url:
                            return url
        # 2. 当前消息附带的图片
        for comp in messages:
            if isinstance(comp, Image):
                url = comp.url or comp.file
                if url:
                    return url
        return None

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command("搜图", alias={"phimg"})
    async def search(self, event: AstrMessageEvent, params: GreedyStr) -> None:
        """通过标签或图片在图站上搜索图片。"""
        scope = self._resolve_scope(event)
        if scope is None:
            yield event.plain_result("搜图仅限群聊使用。")
            return

        opts, tags_text = parse_options(str(params))
        image_url = self.extract_image_url(event)
        has_reply = any(isinstance(c, Reply) for c in event.get_messages())

        # 没有任何输入时显示帮助
        if (
            not tags_text
            and not has_reply
            and not image_url
            and not opts.get("--tags")
            and not opts.get("--status")
        ):
            yield event.plain_result(SEARCH_HELP)
            return

        group_config = await self.get_group_config(scope)

        if opts.get("--status"):
            yield event.plain_result(
                "当前群聊搜图功能状态：\n"
                f"启用：{group_config['enabled']}\n"
                f"标签：{', '.join(group_config['customTags']) or '无'}\n"
                f"全局标签：{'启用' if group_config['useGlobalTags'] else '禁用'}"
            )
            return
        if opts.get("--tags"):
            yield event.plain_result(
                f"当前群聊内置标签：{', '.join(group_config['customTags']) or '无'}"
            )
            return
        if not group_config["enabled"]:
            yield event.plain_result('搜图未在本群开启，管理员请用 "搜图-c --on" 启动')
            return

        # 全角转半角，并去掉可能的 <...> 占位符
        clean_params = re.sub(r"<[^>]+>", "", translate_text(tags_text)).strip()

        try:
            if image_url:
                result = await self._handle_reverse_search(
                    event,
                    image_url,
                    clean_params,
                )
            else:
                result = await self._handle_tag_search(
                    event,
                    opts,
                    clean_params,
                    group_config,
                )
            yield result
        except PhimgError as e:
            yield event.plain_result(str(e))

    async def _handle_reverse_search(
        self,
        event: AstrMessageEvent,
        image_url: str,
        clean_params: str,
    ) -> MessageEventResult:
        """以图搜图，返回待发送的消息结果。"""
        distance = 0.25
        if clean_params:
            try:
                distance = float(clean_params)
            except ValueError:
                return event.plain_result(
                    "图片搜索仅支持数字参数，表示相似度距离（distance）。"
                )

        data = await self.make_request(
            "reverse",
            {
                "key": self.config.get("apiKey", ""),
                "url": image_url,
                "distance": distance,
            },
        )
        images = data.get("images", [])

        if len(images) > 10:
            return event.plain_result(
                f"搜索到过多图片 ({len(images)} 张)，请尝试减小距离参数。"
            )
        if not images:
            return event.plain_result("未找到匹配的图片")

        chain: list[Any] = [
            At(qq=event.get_sender_id()),
            Plain(f"\ndistance: {distance}\n"),
        ]
        for img in images:
            chain.append(self.build_media(img))
            chain.append(Plain(f"\nid: {img.get('id')} | score: {img.get('score')}\n"))
        return event.chain_result(chain)

    async def _handle_tag_search(
        self,
        event: AstrMessageEvent,
        opts: dict[str, Any],
        clean_params: str,
        group_config: dict[str, Any],
    ) -> MessageEventResult:
        """标签搜索，返回待发送的消息结果。"""
        user_tags = (
            [t.strip() for t in clean_params.split(",") if t.strip()]
            if clean_params
            else []
        )
        global_tags = (
            list(self.config.get("defaultTags", []))
            if group_config["useGlobalTags"]
            else []
        )
        group_tags = list(group_config.get("customTags", []))
        all_tags: list[str] = list(
            dict.fromkeys([*group_tags, *global_tags, *user_tags])
        )

        if not all_tags:
            return event.plain_result("请输入搜索标签。")

        per_page = _to_int(opts.get("--pp"), 50)
        page = _to_int(opts.get("--p"), 1)
        query_tags = ", ".join(all_tags)
        data = await self.make_request(
            "images",
            {
                "key": self.config.get("apiKey", ""),
                "q": query_tags,
                "per_page": per_page,
                "page": page,
                "sf": opts.get("--sf", "score"),
                "sd": opts.get("--sd", "desc"),
            },
        )
        images = data.get("images", [])
        if not images:
            return event.plain_result("未找到匹配的图片")

        index = _to_int(opts.get("--i"), -1)
        additional_msg = ""
        if index < 0 or index >= len(images):
            if index >= 0:
                additional_msg = f"索引 {index} 超出单页范围，已随机选择图片"
            index = random.randrange(len(images))

        selected = images[index]
        chain: list[Any] = [
            At(qq=event.get_sender_id()),
            self.build_media(selected),
            Plain(f"\nid: {selected.get('id')} | score: {selected.get('score')}"),
            Plain(f"\ntags: {query_tags}"),
        ]
        if additional_msg:
            chain.append(Plain(f"\n提示：{additional_msg}"))
        return event.chain_result(chain)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("搜图-c", alias={"phimg-c"})
    async def config_cmd(self, event: AstrMessageEvent, params: GreedyStr) -> None:
        """群内搜图配置（仅管理员可用）。"""
        scope = self._resolve_scope(event)
        if scope is None:
            yield event.plain_result("搜图配置仅限群聊使用。")
            return

        opts, _ = parse_options(str(params))
        if not opts:
            yield event.plain_result(CONFIG_HELP)
            return

        group_config = await self.get_group_config(scope)
        responses: list[str] = []

        if opts.get("--on") and opts.get("--off"):
            yield event.plain_result("不能同时开启和关闭搜图功能")
            return
        if opts.get("--onglobal") and opts.get("--offglobal"):
            yield event.plain_result("不能同时开启和关闭全局标签")
            return

        if opts.get("--on"):
            await self.update_group_config(scope, {"enabled": True})
            responses.append("搜图功能已在本群开启")
        elif opts.get("--off"):
            await self.update_group_config(scope, {"enabled": False})
            responses.append("搜图功能已在本群关闭")

        if opts.get("--onglobal"):
            await self.update_group_config(scope, {"useGlobalTags": True})
            responses.append("全局标签已启用")
        elif opts.get("--offglobal"):
            # 关闭全局标签意味着可能搜出非 safe 图片，需要二次确认
            confirm_key = f"{scope}-{event.get_sender_id()}"
            if confirm_key not in self._confirm_off_global:
                self._confirm_off_global.add(confirm_key)
                asyncio.get_running_loop().call_later(
                    60,
                    self._confirm_off_global.discard,
                    confirm_key,
                )
                yield event.plain_result(
                    "关闭全局标签，机器人将会搜出非safe图片\n"
                    "请自行承担风险，再次输入指令确认关闭"
                )
                return
            self._confirm_off_global.discard(confirm_key)
            await self.update_group_config(scope, {"useGlobalTags": False})
            responses.append("全局标签已禁用")

        if opts.get("--add") or opts.get("--rm"):
            new_tags = list(group_config.get("customTags", []))
            if opts.get("--add"):
                add_tags = [
                    t.strip()
                    for t in translate_text(str(opts["--add"])).split(",")
                    if t.strip()
                ]
                new_tags = list(dict.fromkeys([*new_tags, *add_tags]))
            if opts.get("--rm"):
                rm_tags = {
                    t.strip()
                    for t in translate_text(str(opts["--rm"])).split(",")
                    if t.strip()
                }
                new_tags = [t for t in new_tags if t not in rm_tags]
            await self.update_group_config(scope, {"customTags": new_tags})
            responses.append(f"修改成功，本群标签现为: {', '.join(new_tags) or '无'}")

        if not responses:
            # 没有匹配到任何可执行的配置操作，提示用法
            yield event.plain_result(CONFIG_HELP)
            return
        yield event.plain_result("\n".join(responses))


def _to_int(value: Any, default: int) -> int:
    """将值安全地转换为 int，失败时返回默认值。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
