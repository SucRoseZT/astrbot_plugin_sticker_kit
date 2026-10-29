import asyncio
import base64
import hashlib
import json
import mimetypes
import random
import re
import time
import uuid
from pathlib import Path

import aiohttp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
from quart import request as quart_request, send_file

try:
    from astrbot.api.message_components import BaseMessageComponent, ComponentType
except ImportError:
    from astrbot.core.message.components import BaseMessageComponent, ComponentType

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    DATA_ROOT = Path(get_astrbot_data_path())
except Exception:
    DATA_ROOT = Path("data").resolve()

PLUGIN_NAME = "astrbot_plugin_sticker_kit"
PANEL_PREFIX = f"{PLUGIN_NAME}/panel"
API_PREFIX = f"{PLUGIN_NAME}/api"

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

_PROTOCOL_PROMPT = (
    "[表情包能力] 你可以在回复里配一张表情包（不要每条都配，只在语境合适、或用户点名要表情包时用）。"
    "方法：把回复正文照常写完，然后在最末尾另起一行输出标签：\n"
    '<sticker>{"query":"三五个关键词"}</sticker>\n'
    "query 写想要的情绪/画面/内容关键词（如「得意 自夸」「委屈 委巴巴」），"
    "系统会本地检索最匹配的一张图并自动追加在回复后，用户看不到标签本身。"
    "没有合适的就不输出标签；不要输出标签以外的任何表情占位格式。"
)

_TAG_RE = re.compile(
    r"(?:<|&lt;|\\<)\s*sticker\s*(?:>|&gt;|\\>)(.*?)"
    r"(?:<|&lt;|\\<)\s*/\s*sticker\s*(?:>|&gt;|\\>)",
    re.IGNORECASE | re.DOTALL,
)


class _QQFaceImage(BaseMessageComponent):
    """OneBot image segment with emoji subtype flag，让 QQ 客户端按表情包样式渲染。"""

    type: ComponentType = ComponentType.Image
    file: str
    path: str
    url: str = ""
    sub_type: int = 1
    payload_file: str

    def __init__(self, path: str) -> None:
        resolved = str(Path(path).resolve(strict=True))
        payload = base64.b64encode(Path(resolved).read_bytes()).decode("ascii")
        super().__init__(
            file=resolved,
            path=resolved,
            url="",
            sub_type=1,
            payload_file=f"base64://{payload}",
        )

    def toDict(self) -> dict:
        return {
            "type": "image",
            "data": {"file": self.payload_file, "sub_type": self.sub_type},
        }

    async def to_dict(self) -> dict:
        return self.toDict()

_PROMPT = (
    "你是表情包标注员。看这张表情包图片，输出 JSON（不要 markdown 代码块）：\n"
    '{"name": "简短名称(<=12字)", "tags": ["标签","标签","标签"], '
    '"emotions": ["情绪1","情绪2"], "intents": ["使用场合1","使用场合2"], '
    '"description": "客观描述画面内容(30-80字)", "visible_text": "图上出现的文字，没有则空串"}\n'
    "tags 3-6 个，包含画面主体、角色特征、风格；emotions 是这张图表达的情绪；"
    "intents 是适合在什么对话场景下发送。"
)

_COMPANION_CATALOG = (
    DATA_ROOT
    / "plugin_data"
    / "astrbot_plugin_private_companion"
    / "reaction_expression_library"
    / "catalog.json"
)
_COMPANION_IMAGES = _COMPANION_CATALOG.parent / "images"


def _now() -> float:
    return time.time()


def _repair_truncated_json(s: str) -> str:
    """补全被 max_tokens 截断的 JSON：闭合未完成字符串并补齐缺失括号。"""
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        s += '"'
    s = s.rstrip()
    if s.endswith(","):
        s = s[:-1]
    elif s.endswith(":"):
        s += " null"
    return s + "".join(reversed(stack))


def _json_candidates(frag: str):
    """从截断片段生成候选：原文 + 逐级回退到最后一个逗号/括号。"""
    s = frag
    for _ in range(6):
        if s:
            yield s
        i_comma, i_obj, i_arr = s.rfind(","), s.rfind("{"), s.rfind("[")
        cut = max(i_comma, i_obj, i_arr)
        if cut <= 0:
            return
        s = s[:cut] if cut == i_comma else s[: cut + 1]


class StickerKit(Star):
    _SEND_FIELDS = ("sticker_send", "sticker_search", "sticker_list")

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self._data_dir = DATA_ROOT / "plugin_data" / PLUGIN_NAME
        self._img_dir = self._data_dir / "images"
        self._index_path = self._data_dir / "index.json"
        self._img_dir.mkdir(parents=True, exist_ok=True)
        self._items: list[dict] = []
        self._lock = asyncio.Lock()
        self._load_index()
        self._migrated = 0
        self._migrate_companion()
        self._write_lock_until = 0.0
        self._last_send: dict[str, float] = {}
        self._analysis_tasks: dict[str, asyncio.Task] = {}
        self._register_panel()

    # ---------- 存储 ----------

    def _load_index(self) -> None:
        try:
            self._items = json.loads(self._index_path.read_text(encoding="utf-8"))
        except Exception:
            self._items = []

    def _save_index(self) -> None:
        tmp = self._index_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._items, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(self._index_path)

    def _migrate_companion(self) -> None:
        if not _COMPANION_CATALOG.exists():
            return
        try:
            catalog = json.loads(_COMPANION_CATALOG.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"[StickerKit] 迁移：读取陪伴插件 catalog 失败 {exc}")
            return
        known_shas = {i.get("sha256") for i in self._items if i.get("sha256")}
        count = 0
        for src in catalog.get("items", []):
            sha = str(src.get("sha256") or "").lower()
            if not sha or sha in known_shas or not src.get("enabled", True):
                continue
            src_file = _COMPANION_IMAGES / str(src.get("stored_name") or "")
            if not src_file.exists():
                continue
            ext = src_file.suffix.lower()
            new_name = f"{uuid.uuid4().hex}{ext}"
            try:
                (self._img_dir / new_name).write_bytes(src_file.read_bytes())
            except Exception as exc:
                logger.warning(f"[StickerKit] 迁移：复制 {src_file.name} 失败 {exc}")
                continue
            self._items.append(
                {
                    "id": uuid.uuid4().hex,
                    "stored_name": new_name,
                    "sha256": sha,
                    "name": str(src.get("name") or "未命名")[:40],
                    "tags": [str(t) for t in (src.get("tags") or [])][:8],
                    "emotions": [str(t) for t in (src.get("emotions") or [])][:6],
                    "intents": [str(t) for t in (src.get("intents") or [])][:6],
                    "description": str(src.get("description") or "")[:200],
                    "visible_text": str(src.get("visible_text") or "")[:200],
                    "enabled": True,
                    "size": int(src.get("size") or 0),
                    "mime": mimetypes.guess_type(src_file.name)[0] or "image/jpeg",
                    "analysis_status": "complete",
                    "analysis_error": "",
                    "source": "migrated_companion",
                    "created_at": _now(),
                }
            )
            known_shas.add(sha)
            count += 1
        if count:
            self._save_index()
        self._migrated = count
        if count:
            logger.info(f"[StickerKit] 已从陪伴插件表情库迁移 {count} 张图（含全部文字标注，零 LLM 调用）")

    # ---------- 工具闸口 ----------

    def _admin_check(self, event: AstrMessageEvent) -> str:
        if self.config.get("admin_only", True) and not event.is_admin():
            return "权限拒绝：只有管理员可以触发表情包功能。"
        return ""

    def _feature_check(self, action: str) -> str:
        remain = self._write_lock_until - _now()
        if remain > 0:
            return (
                f"发送冷却中（还剩 {remain:.0f} 秒）：上一次表情包发送刚成功，结果已确认有效，"
                "本轮不要再调用任何表情包工具，请直接用文字向用户汇报结果。"
            )
        if self.config.get(f"enable_{action}", True):
            return ""
        return f"该功能已关闭（{action}）：请直接告知用户此功能未开启，不要尝试绕过。"

    def _engage_lock(self) -> None:
        secs = float(self.config.get("write_lock_seconds", 60) or 0)
        if secs > 0:
            self._write_lock_until = _now() + secs

    def _send_cooldown(self, event: AstrMessageEvent) -> str:
        secs = float(self.config.get("send_cooldown_seconds", 30) or 0)
        if secs <= 0:
            return ""
        slot = event.unified_msg_origin
        last = self._last_send.get(slot)
        if last is not None and _now() - last < secs:
            return f"发送太频繁：冷却中（还剩 {secs - (_now() - last):.0f} 秒），本次未执行。"
        return ""

    def _cooldown_hit(self, event: AstrMessageEvent) -> bool:
        secs = float(self.config.get("send_cooldown_seconds", 30) or 0)
        if secs <= 0:
            return False
        last = self._last_send.get(event.unified_msg_origin)
        return last is not None and _now() - last < secs

    def _probability_hit(self) -> bool:
        try:
            prob = float(self.config.get("send_probability", 1.0))
        except (TypeError, ValueError):
            prob = 1.0
        prob = min(1.0, max(0.0, prob))
        return prob >= 1.0 or random.random() < prob

    def _build_image_component(self, event: AstrMessageEvent, path: Path):
        if str(self.config.get("image_format") or "image").lower() == "qq_emoji":
            try:
                if str(event.get_platform_name() or "") == "aiocqhttp":
                    return _QQFaceImage(str(path))
            except Exception as exc:
                logger.warning(f"[StickerKit] QQ表情格式组件构建失败，回退普通图片: {type(exc).__name__}")
        try:
            return Image.fromFileSystem(str(path))
        except AttributeError:
            return Image.from_file_system(str(path))

    def _mark_sent(self, event: AstrMessageEvent, item: dict) -> None:
        self._last_send[event.unified_msg_origin] = _now()
        item["usage_count"] = int(item.get("usage_count") or 0) + 1
        item["last_used_at"] = _now()
        self._save_index()

    # ---------- 检索 ----------

    @staticmethod
    def _keywords(query: str) -> list[str]:
        words = [w for w in re.split(r"[\s,，。.！!？?、/|]+", str(query or "").strip()) if w]
        if not words:
            return []
        extra = []
        for w in words:
            if len(w) > 2 and not w.isascii():
                extra.extend(w[i : i + 2] for i in range(len(w) - 1))
        return list(dict.fromkeys(words + extra))

    @classmethod
    def _score_item(cls, keywords: list[str], raw_query: str, item: dict) -> float:
        if not item.get("enabled", True):
            return -1.0
        fields = {
            "name": (str(item.get("name") or ""), 4.0),
            "tags": (" ".join(item.get("tags") or []), 2.5),
            "emotions": (" ".join(item.get("emotions") or []), 2.0),
            "intents": (" ".join(item.get("intents") or []), 2.0),
            "description": (str(item.get("description") or ""), 1.0),
            "visible_text": (str(item.get("visible_text") or ""), 1.0),
        }
        score = 0.0
        for kw in keywords:
            for _, (text, weight) in fields.items():
                if kw and kw in text:
                    score += weight
        if raw_query and len(raw_query) >= 2:
            for text, weight in fields.values():
                if raw_query in text:
                    score += weight * 0.5
        return score

    def _search(self, query: str, top_k: int) -> list[tuple[float, dict]]:
        keywords = self._keywords(query)
        scored = [
            (self._score_item(keywords, query, it), it)
            for it in self._items
        ]
        scored = [(s, it) for s, it in scored if s > 0]
        scored.sort(key=lambda pair: -pair[0])
        return scored[: max(1, int(top_k or 6))]

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="sticker_search")
    async def sticker_search(self, event: AstrMessageEvent, query: str):
        """按对话语境检索表情包，返回最匹配的候选列表（含 sticker_id 和描述）。

        优先用快速通道：直接在回复末尾输出 <sticker>{"query":"关键词"}</sticker> 即可自动配图，无需调用本工具。
        本工具仅在需要精确挑选（检索后用 sticker_pick 指定发送）时调用；没有合适的就直接文字回复，不要硬选。
        Args:
            query(string): 检索需求，写清情绪/场景/内容，如「得意 叉腰 求夸」「委屈 委巴巴」
        """
        denied = self._admin_check(event)
        if denied:
            return denied
        blocked = self._feature_check("search")
        if blocked:
            return blocked
        query = str(query or "").strip()
        if not query:
            return "缺少检索需求：请用 query 描述想要的情绪/场景。"
        hits = self._search(query, self.config.get("top_k", 6))
        if not hits:
            return "没有匹配到表情包。不要硬选，直接用文字回复即可；也可以换个更贴近库存描述的说法再试一次（仅一次）。"
        lines = [f"共 {len(hits)} 个候选，选一个最贴语境的，用 sticker_pick(sticker_id, caption) 发送："]
        for rank, (score, it) in enumerate(hits, 1):
            desc = str(it.get("description") or "")[:60]
            emo = "/".join((it.get("emotions") or [])[:3])
            lines.append(f"#{rank} sticker_id={it['id']} 名称={it.get('name')} 情绪={emo}")
            lines.append(f"    描述：{desc}")
        lines.append("发送时 caption 必须是一条完整、自然、可独立成立的正文，图片只是追加在正文后。")
        return "\n".join(lines)

    @filter.llm_tool(name="sticker_pick")
    async def sticker_pick(self, event: AstrMessageEvent, sticker_id: str, caption: str):
        """发送指定的表情包图片。必须先用 sticker_search 拿到 sticker_id，禁止凭空填写。

        仅管理员可用；caption 是随图发出的完整文字正文，不能为空、不能用「请忽略/测试」类占位内容。
        Args:
            sticker_id(string): sticker_search 返回的 sticker_id，逐字符照抄
            caption(string): 随图发出的完整正文
        """
        denied = self._admin_check(event)
        if denied:
            return denied
        blocked = self._feature_check("send")
        if blocked:
            return blocked
        cooldown_hit = self._send_cooldown(event)
        if cooldown_hit:
            return cooldown_hit
        caption = str(caption or "").strip()
        if not caption or re.search(r"忽略|误发|误触|测试消息|占位", caption, re.IGNORECASE):
            return "caption 缺失或无意义（请忽略/测试类）：请提供一条完整自然的正文，图片不能替代正文。"
        sticker_id = str(sticker_id or "").strip()
        item = next((it for it in self._items if it["id"] == sticker_id), None)
        if item is None or not item.get("enabled", True):
            return f"sticker_id 无效或已停用：{sticker_id[:40]}。请从 sticker_search 的结果里逐字符照抄，不要凭记忆填。"
        path = self._img_dir / item["stored_name"]
        if not path.exists():
            return "表情包文件丢失，请提醒管理员在面板重新上传。"
        try:
            image_component = self._build_image_component(event, path)
        except Exception as exc:
            return f"图片组件构造失败：{exc}"
        try:
            chain = MessageChain([Plain(caption), image_component])
            result = await self.context.send_message(event.unified_msg_origin, chain)
            if result is False:
                return "发送失败：平台返回 False，请如实告知用户发送未成功。"
        except Exception as exc:
            return f"发送失败：{exc}"
        self._mark_sent(event, item)
        self._engage_lock()
        return f"表情包「{item.get('name')}」已随正文发送成功。到此为止，不要重复发送，也不要再调用任何表情包工具验证。"

    @filter.llm_tool(name="sticker_list")
    async def sticker_list(self, event: AstrMessageEvent, page: int = 1):
        """浏览表情包库存列表（名称+描述摘要）。

        仅管理员可用；仅在主人想了解表情包库存时调用。
        Args:
            page(number): 页码，从 1 开始，每页 20 条
        """
        denied = self._admin_check(event)
        if denied:
            return denied
        blocked = self._feature_check("search")
        if blocked:
            return blocked
        items = [it for it in self._items if it.get("enabled", True)]
        if not items:
            return "表情包库存为空，请管理员在面板上传。"
        page = max(1, int(page or 1))
        per = 20
        chunk = items[(page - 1) * per : page * per]
        if not chunk:
            return f"第 {page} 页为空（共 {len(items)} 张，{(len(items) + per - 1) // per} 页）。"
        lines = [f"表情包库存（第 {page} 页，共 {len(items)} 张）："]
        for it in chunk:
            lines.append(f"- {it.get('name')} | 情绪={'/'.join((it.get('emotions') or [])[:3])} | {str(it.get('description') or '')[:50]}")
        return "\n".join(lines)

    # ---------- 快速通道：协议标签配图 ----------

    @filter.on_llm_request()
    async def inject_sticker_protocol(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.config.get("protocol_mode", True):
            return
        if not self.config.get("enable_send", True):
            return
        if self.config.get("admin_only", True) and not event.is_admin():
            return
        try:
            req.system_prompt = (req.system_prompt or "") + "\n" + _PROTOCOL_PROMPT
        except Exception:
            pass

    @filter.on_decorating_result()
    async def attach_sticker_from_tag(self, event: AstrMessageEvent, *args, **kwargs):
        result = event.get_result()
        chain = list(getattr(result, "chain", []) or []) if result is not None else []
        if not chain:
            return
        stripped = False
        matched_query = ""
        for comp in chain:
            if not isinstance(comp, Plain) or not isinstance(comp.text, str):
                continue
            m = _TAG_RE.search(comp.text)
            if not m:
                continue
            stripped = True
            if not matched_query:
                matched_query = m.group(1).strip()
            comp.text = _TAG_RE.sub("", comp.text)
            comp.text = re.sub(r"\n{3,}", "\n\n", comp.text).rstrip()
        if not stripped:
            return
        send = self.config.get("enable_send", True)
        if send and self.config.get("admin_only", True) and not event.is_admin():
            send = False
        if send and self._cooldown_hit(event):
            send = False
        if send and not self._probability_hit():
            send = False
            logger.info("[StickerKit] 快速通道概率未命中，本次仅发文字")
        item = None
        if send:
            query = matched_query
            try:
                payload = json.loads(query)
                if isinstance(payload, dict):
                    query = str(payload.get("query") or "")
            except Exception:
                pass
            hits = self._search(query, 1) if query else []
            if hits:
                item = hits[0][1]
            else:
                logger.info(f"[StickerKit] 快速通道未命中图（query={query[:40]!r}），仅剥离标签")
        if item is None:
            try:
                result.chain = chain
            except Exception:
                pass
            return
        path = self._img_dir / item["stored_name"]
        if not path.exists():
            try:
                result.chain = chain
            except Exception:
                pass
            return
        try:
            image_component = self._build_image_component(event, path)
        except Exception as exc:
            logger.warning(f"[StickerKit] 快速通道图片组件构建失败: {type(exc).__name__}")
            try:
                result.chain = chain
            except Exception:
                pass
            return
        mode = str(self.config.get("delivery_mode") or "separate_after").lower()
        if mode not in {"separate_after", "same_message"}:
            mode = "separate_after"
        if mode == "same_message":
            chain.append(image_component)
            try:
                result.chain = chain
            except Exception:
                event.set_result(event.chain_result(chain))
            self._mark_sent(event, item)
            logger.info(
                f"[StickerKit] 快速通道配图成功(同消息): {item.get('name')} query={matched_query[:40]!r}"
            )
            return
        try:
            setattr(
                event,
                "_sticker_kit_pending_attachment",
                {"component": image_component, "item": item},
            )
        except Exception:
            chain.append(image_component)
            try:
                result.chain = chain
            except Exception:
                event.set_result(event.chain_result(chain))
            self._mark_sent(event, item)
            return
        logger.info(
            f"[StickerKit] 快速通道待补发(正文后单独一条): {item.get('name')} query={matched_query[:40]!r}"
        )

    @filter.after_message_sent()
    async def send_sticker_after_reply(self, event: AstrMessageEvent, *args, **kwargs):
        """正文回复发出后，把挂起的表情包作为独立一条消息补发。"""
        pending = getattr(event, "_sticker_kit_pending_attachment", None)
        if not isinstance(pending, dict) or pending.get("done"):
            return
        pending["done"] = True
        component = pending.get("component")
        item = pending.get("item")
        if component is None or not isinstance(item, dict):
            return
        try:
            send_result = await event.send(event.chain_result([component]))
            sent = send_result is not False
        except Exception as exc:
            logger.warning(f"[StickerKit] 表情包单独补发失败: {type(exc).__name__} {exc}")
            return
        if sent:
            self._mark_sent(event, item)
            logger.info(f"[StickerKit] 表情包已单独补发: {item.get('name')}")
        else:
            logger.warning("[StickerKit] 表情包单独补发返回 False")

    # ---------- 管理命令 ----------

    @filter.command("sticker")
    async def sticker_cmd(self, event: AstrMessageEvent):
        """表情包插件管理：/sticker status"""
        if not event.is_admin():
            return
        msg = event.message_str.strip()
        sub = msg.split()[1] if len(msg.split()) > 1 else "status"
        if sub == "status":
            total = len(self._items)
            analyzing = sum(1 for it in self._items if it.get("analysis_status") == "pending")
            yield event.plain_result(
                f"StickerKit v1.1.0：库存 {total} 张（本次启动迁移自陪伴插件 {self._migrated} 张），"
                f"打标中 {analyzing}，快速通道={'开' if self.config.get('protocol_mode', True) else '关'}，"
                f"概率={float(self.config.get('send_probability', 1.0)):.0%}，"
                f"投递={self.config.get('delivery_mode', 'separate_after')}，格式={self.config.get('image_format', 'image')}，"
                f"面板地址：WebUI → /api/plug/{PANEL_PREFIX}"
            )
        elif sub == "reload":
            self._load_index()
            yield event.plain_result(f"索引已重载：{len(self._items)} 条")

    # ---------- 打标 ----------

    def _llm_conf(self) -> tuple[str, str, str]:
        return (
            str(self.config.get("llm_api_base") or "").strip().rstrip("/"),
            str(self.config.get("llm_api_key") or "").strip(),
            str(self.config.get("llm_model") or "").strip(),
        )

    async def _analyze_item(self, item: dict) -> None:
        base, key, model = self._llm_conf()
        if not (base and key and model):
            return
        path = self._img_dir / item["stored_name"]
        try:
            raw = path.read_bytes()
        except Exception as exc:
            item["analysis_status"] = "failed"
            item["analysis_error"] = f"读取失败 {exc}"[:200]
            return
        mime = item.get("mime") or mimetypes.guess_type(str(path))[0] or "image/jpeg"
        data_url = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "max_tokens": 1200,
        }
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

        async def _call_once() -> dict:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
                async with session.post(f"{base}/chat/completions", json=payload, headers=headers) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
                    content = json.loads(text)["choices"][0]["message"]["content"]
            content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
            content = re.sub(r"```(?:json)?", "", content).strip()
            m = re.search(r"\{.*", content, re.S)
            if not m:
                raise ValueError(f"返回不含 JSON：{content[:150]}")
            meta = None
            for cand in _json_candidates(m.group(0)):
                fixed = _repair_truncated_json(cand)
                for v in (fixed, fixed.replace("，", ",").replace("：", ":")):
                    try:
                        meta = json.loads(v)
                        break
                    except Exception:
                        continue
                if isinstance(meta, dict):
                    break
            if not isinstance(meta, dict):
                raise ValueError(f"返回不是有效 JSON：{content[:150]}")
            return meta

        last_err = ""
        for attempt in range(2):
            try:
                meta = await _call_once()
                break
            except Exception as exc:
                last_err = str(exc)[:200]
                if attempt == 0:
                    await asyncio.sleep(2)
        else:
            item["analysis_status"] = "failed"
            item["analysis_error"] = last_err
            return
        item["name"] = str(meta.get("name") or item.get("name") or "未命名")[:40]
        item["tags"] = [str(t)[:20] for t in (meta.get("tags") or [])][:8]
        item["emotions"] = [str(t)[:20] for t in (meta.get("emotions") or [])][:6]
        item["intents"] = [str(t)[:20] for t in (meta.get("intents") or [])][:6]
        item["description"] = str(meta.get("description") or "")[:200]
        item["visible_text"] = str(meta.get("visible_text") or "")[:200]
        item["analysis_status"] = "complete"
        item["analysis_error"] = ""
        self._save_index()

    def _queue_analysis(self, item: dict) -> None:
        base, key, model = self._llm_conf()
        if not (base and key and model):
            return
        item["analysis_status"] = "pending"
        task = asyncio.ensure_future(self._analyze_item(item))

        def _done(fut: asyncio.Task) -> None:
            self._analysis_tasks.pop(item["id"], None)
            self._save_index()

        task.add_done_callback(_done)
        self._analysis_tasks[item["id"]] = task

    # ---------- 面板 Web API ----------

    def _register_panel(self) -> None:
        register = self.context.register_web_api
        register(f"{PANEL_PREFIX}", self._panel_page, ["GET"], "StickerKit 面板")
        register(f"{API_PREFIX}/list", self._api_list, ["GET"], "表情包列表")
        register(f"{API_PREFIX}/upload", self._api_upload, ["POST"], "批量上传")
        register(f"{API_PREFIX}/update", self._api_update, ["POST"], "修改标注")
        register(f"{API_PREFIX}/delete", self._api_delete, ["POST"], "删除")
        register(f"{API_PREFIX}/image", self._api_image, ["GET"], "图片预览")
        register(f"{API_PREFIX}/analyze", self._api_analyze, ["POST"], "重新打标")

    async def _panel_page(self):
        page = Path(__file__).parent / "pages" / "表情包面板" / "index.html"
        return await send_file(str(page))

    def _ok(self, data=None) -> dict:
        return {"status": "ok", "data": data or {}}

    def _err(self, msg: str) -> dict:
        return {"status": "error", "message": msg}

    async def _api_list(self):
        items = [
            {
                "id": it["id"],
                "name": it.get("name"),
                "tags": it.get("tags"),
                "emotions": it.get("emotions"),
                "intents": it.get("intents"),
                "description": it.get("description"),
                "visible_text": it.get("visible_text"),
                "enabled": it.get("enabled", True),
                "size": it.get("size"),
                "mime": it.get("mime"),
                "analysis_status": it.get("analysis_status", "none"),
                "analysis_error": it.get("analysis_error", ""),
                "source": it.get("source", ""),
                "usage_count": it.get("usage_count", 0),
                "created_at": it.get("created_at"),
            }
            for it in sorted(self._items, key=lambda x: -float(x.get("created_at") or 0))
        ]
        llm_ready = all(self._llm_conf())
        return {"status": "ok", "data": {"items": items, "llm_ready": llm_ready}}

    async def _api_upload(self):
        all_files = await quart_request.files
        files = all_files.getlist("files") or all_files.getlist("file")
        if not files:
            return self._err("没有收到文件")
        max_bytes = int(self.config.get("max_image_mb", 10) or 10) * 1024 * 1024
        known_shas = {it.get("sha256") for it in self._items}
        saved, dup, rejected = [], [], []
        for f in files:
            ext = Path(f.filename or "").suffix.lower()
            if ext not in ALLOWED_EXT:
                rejected.append(f"{f.filename}: 不支持的格式")
                continue
            raw = f.read()
            if len(raw) > max_bytes:
                rejected.append(f"{f.filename}: 超过 {max_bytes // 1024 // 1024}MB")
                continue
            if not raw[:8]:
                rejected.append(f"{f.filename}: 空文件")
                continue
            sha = hashlib.sha256(raw).hexdigest()
            if sha in known_shas:
                dup.append(f.filename)
                continue
            stored = f"{uuid.uuid4().hex}{ext}"
            (self._img_dir / stored).write_bytes(raw)
            item = {
                "id": uuid.uuid4().hex,
                "stored_name": stored,
                "sha256": sha,
                "name": Path(f.filename or stored).stem[:40],
                "tags": [],
                "emotions": [],
                "intents": [],
                "description": str((await quart_request.form).get("description") or "")[:200],
                "visible_text": "",
                "enabled": True,
                "size": len(raw),
                "mime": mimetypes.guess_type(f.filename or "")[0] or "image/jpeg",
                "analysis_status": "none",
                "analysis_error": "",
                "source": "upload",
                "created_at": _now(),
            }
            self._items.append(item)
            known_shas.add(sha)
            saved.append(item)
            if not item["description"]:
                self._queue_analysis(item)
        if saved:
            self._save_index()
        return self._ok(
            {
                "saved": len(saved),
                "duplicated": dup,
                "rejected": rejected,
                "ids": [it["id"] for it in saved],
            }
        )

    def _find(self, sticker_id: str) -> dict | None:
        return next((it for it in self._items if it["id"] == str(sticker_id)), None)

    async def _api_update(self):
        payload = await quart_request.get_json(silent=True) or {}
        item = self._find(payload.get("id") or "")
        if item is None:
            return self._err("找不到该表情包")
        for field in ("name", "description", "visible_text"):
            if field in payload:
                item[field] = str(payload[field] or "")[:200 if field != "name" else 40]
        for field in ("tags", "emotions", "intents"):
            if field in payload:
                item[field] = [str(t)[:20] for t in (payload[field] or [])][:8]
        if "enabled" in payload:
            item["enabled"] = bool(payload["enabled"])
        self._save_index()
        return self._ok()

    async def _api_delete(self):
        payload = await quart_request.get_json(silent=True) or {}
        item = self._find(payload.get("id") or "")
        if item is None:
            return self._err("找不到该表情包")
        task = self._analysis_tasks.pop(item["id"], None)
        if task:
            task.cancel()
        try:
            (self._img_dir / item["stored_name"]).unlink(missing_ok=True)
        except Exception:
            pass
        self._items.remove(item)
        self._save_index()
        return self._ok()

    async def _api_image(self):
        item = self._find(quart_request.args.get("id") or "")
        if item is None:
            return self._err("找不到该表情包")
        path = self._img_dir / item["stored_name"]
        if not path.exists():
            return self._err("文件丢失")
        if quart_request.args.get("b64"):
            mime = item.get("mime") or "image/jpeg"
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            return self._ok({"b64": f"data:{mime};base64,{data}"})
        resp = await send_file(str(path))
        resp.headers["Cache-Control"] = "private, max-age=86400"
        return resp

    async def _api_analyze(self):
        payload = await quart_request.get_json(silent=True) or {}
        item = self._find(payload.get("id") or "")
        if item is None:
            return self._err("找不到该表情包")
        base, key, model = self._llm_conf()
        if not (base and key and model):
            return self._err("请先在插件配置里填写 LLM API（base/key/model 三项）")
        self._queue_analysis(item)
        return self._ok({"queued": True})

    async def terminate(self):
        for task in self._analysis_tasks.values():
            task.cancel()
        self._analysis_tasks.clear()
