import asyncio
import base64
import difflib
import hashlib
import io
import json
import mimetypes
import random
import re
import time
import uuid
from collections import deque
from pathlib import Path

try:  # 打标前缩放图片用；缺失时自动退回原图，不阻塞插件加载
    from PIL import Image as _PILImage

    _RESAMPLE = getattr(getattr(_PILImage, "Resampling", _PILImage), "LANCZOS")
except Exception:  # pragma: no cover
    _PILImage = None
    _RESAMPLE = None

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

_PROTOCOL_MARK = "[表情包能力]"
_STEAL_MARK = "【偷表情包】"

DEFAULT_NAMED_KEYWORDS = "表情包,斗图,发个图,发张图,来张图,配个图,发表情,来一张"

# 频档 → 提示词语气。全部使用正向引导，不再出现「不要每条都配」这类否定起手，
# 避免小模型把「默认不发」当成结论。off 表示不注入协议。
_FREQ_POLICY = {
    "off": "",
    "rare": "配图从简：只有用户明确索要表情包时才配一张，其余回复不配图。",
    "normal": "自然配图：语境合适时配一张，大致每三四条回复一次；用户要表情包时一定配上。",
    "often": "积极配图：绝大多数回复都配一张，只有严肃话题、道歉、长篇说明这类场合才不配。",
    "always": "每条回复都配一张表情包，仅当内容必须严肃或需要长篇说明时例外。",
}

# 面板里是中文选项，这里同时接受中文与英文别名，避免手改配置文件时值失效。
_FREQ_ALIAS = {
    "": "normal",
    "关闭": "off",
    "禁用": "off",
    "off": "off",
    "none": "off",
    "0": "off",
    "很少": "rare",
    "少": "rare",
    "rare": "rare",
    "low": "rare",
    "适中": "normal",
    "默认": "normal",
    "normal": "normal",
    "medium": "normal",
    "较多": "often",
    "多": "often",
    "often": "often",
    "high": "often",
    "每条都发": "always",
    "每条": "always",
    "always": "always",
}

_FREQ_LABELS = {
    "off": "关闭",
    "rare": "很少",
    "normal": "适中",
    "often": "较多",
    "always": "每条都发",
}


def _protocol_body(level: str, extra: str = "", with_vocab: bool = True) -> str:
    """按频档拼出协议正文；extra 为用户自定义的附加约束。

    with_vocab=False 用于已经注入「库存概览」的情况：那时库存里实际有哪些情绪
    已经写在下面了，再说一遍内置词表纯属重复，白占每轮 token。
    """
    policy = _FREQ_POLICY.get(level, _FREQ_POLICY["normal"])
    vocab = (
        "尽量用词表里有的词（开心 大笑 微笑 害羞 撒娇 卖萌 得意 兴奋 期待 惊讶 疑惑 无语 无奈 "
        "嫌弃 尴尬 委屈 难过 大哭 生气 冷漠 疲惫 害怕 嘲讽 挑衅 温柔 安慰 鼓励 敷衍 拒绝 求饶 认同 感谢）；"
        if with_vocab
        else "尽量用下方库存里实际有的词；"
    )
    body = (
        f"{_PROTOCOL_MARK} {policy}"
        "写法：把正文照常写完，然后在最末尾另起一行输出标签：\n"
        '<sticker>{"query":"三五个关键词"}</sticker>\n'
        "query 用常见情绪词 + 画面词，如「得意 自夸」「委屈」「震惊」「猫 无语」，"
        f"{vocab}"
        "系统会本地检索最匹配的一张图并自动追加在回复后，用户看不到标签本身。"
        "没有合适的图就不输出标签，系统会自动只发文字；除这个标签外不要使用其他表情占位格式。"
    )
    extra = str(extra or "").strip()
    if extra:
        body = f"{body}\n{extra}"
    return body


def _est_tokens(text: str) -> int:
    """粗估 token 数，只用于预算控制。

    不引入 tokenizer 依赖（各家模型分词器不同，装了也不准）。中文按 1 字≈1 token、
    其余字符按 0.5 估算 —— 宁可高估，避免"以为没超，实际超了"。
    """
    s = str(text or "")
    if not s:
        return 0
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff")
    return int(cjk + (len(s) - cjk) * 0.5 + 0.999)

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

# 受控情绪词表：打标只能从这里选，避免小模型自由发挥造出「委屈巴巴 / 恳求 / 期待」
# 这类邻近语义污染词，也避免同义词堆叠（无语/沉默/嫌弃 其实是一个判断）。
_EMOTION_VOCAB = (
    "开心,大笑,微笑,害羞,撒娇,卖萌,得意,兴奋,期待,惊讶,疑惑,无语,无奈,嫌弃,尴尬,"
    "委屈,难过,大哭,生气,冷漠,疲惫,害怕,嘲讽,挑衅,威胁,温柔,安慰,鼓励,敷衍,拒绝,"
    "求饶,认同,感谢,打招呼,告别,沉思,发呆,偷笑,奸笑,崩溃,怀疑,其他"
)
_EMOTION_SET = {w for w in _EMOTION_VOCAB.split(",") if w}

# 常见口语 → 词表内标准词。AI 写 query 时用的是大白话（呆萌/懵逼/心累），
# 词表里是标准词（卖萌/疑惑/疲惫），没有这张表就会出现「库存里有图却搜不到」。
_ALIAS = {
    "呆萌": "卖萌", "可爱": "卖萌", "萌": "卖萌", "软萌": "卖萌",
    "懵逼": "疑惑", "困惑": "疑惑", "不解": "疑惑", "问号": "疑惑",
    "心累": "疲惫", "摆烂": "疲惫", "累": "疲惫", "困": "疲惫",
    "火大": "生气", "愤怒": "生气", "抓狂": "生气", "气死": "生气", "生气气": "生气",
    "伤心": "难过", "沮丧": "难过", "低落": "难过", "emo": "难过",
    "哭": "大哭", "爆哭": "大哭", "泪": "大哭", "流泪": "大哭",
    "高兴": "开心", "快乐": "开心", "好耶": "开心",
    "谢谢": "感谢", "谢": "感谢",
    "拜拜": "告别", "再见": "告别", "晚安": "告别",
    "你好": "打招呼", "嗨": "打招呼", "在吗": "打招呼",
    "求": "求饶", "拜托": "求饶", "求求": "求饶",
    "赞": "认同", "厉害": "认同", "牛": "认同", "同意": "认同",
    "阴阳": "嘲讽", "阴阳怪气": "嘲讽", "呵呵": "嘲讽",
    "慌": "害怕", "恐惧": "害怕", "吓": "害怕", "瑟瑟发抖": "害怕",
    "沉默": "无语", "服了": "无语", "绝了": "无语",
    "嫌弃脸": "嫌弃", "白眼": "嫌弃",
    "委屈巴巴": "委屈", "想哭": "委屈",
    "鼓励": "鼓励", "加油": "鼓励",
    "抱抱": "安慰", "摸摸": "安慰",
    # 旧库的 tags 里常混着情绪词（旧提示词让 AI 随便写），补一层别名让它们还能被检索到
    "自夸": "得意", "臭美": "得意", "炫耀": "得意", "得瑟": "得意", "装": "得意",
    "俏皮": "卖萌", "调皮": "卖萌",
    "调侃": "嘲讽", "吐槽": "嘲讽", "讽刺": "嘲讽", "嘲笑": "嘲讽",
    "震惊": "惊讶", "吃惊": "惊讶",
    "郁闷": "难过", "不开心": "难过", "悲伤": "难过", "丧": "难过",
    "暴躁": "生气", "恼火": "生气", "怒气": "生气",
    "惊恐": "害怕", "紧张": "害怕", "焦虑": "害怕",
    "脸红": "害羞", "羞": "害羞",
    "汗": "无语", "服气": "无语",
    "笑嘻嘻": "微笑", "笑": "微笑",
    "激动": "兴奋", "宠溺": "温柔", "贴心": "温柔",
    "赞同": "认同", "点赞": "认同", "点头": "认同",
    "摆手": "拒绝", "不用": "拒绝",
    "招手": "打招呼", "hi": "打招呼",
    "跪": "求饶", "救命": "求饶",
    "压力": "疲惫", "加班": "疲惫", "熬夜": "疲惫",
}

# 打标提示词：输出字段刻意压到最少（name/emotions/tags/description/visible_text/confidence），
# 并且强制「主情绪只能一个、次情绪不确定就留空」，从源头减少错标。
_PROMPT = (
    "你是表情包标注员。看这张图，只输出一个 JSON，不要解释、不要 markdown 代码块。\n"
    '{"name":"简短名称(<=10字)","emotions":["主情绪"],"tags":["画面关键词"],'
    '"description":"客观描述画面(20-50字)","visible_text":"图上文字，没有则空串",'
    '"confidence":0.8}\n'
    "规则（必须遵守）：\n"
    f"1. emotions 的第一个必须是这张图最主要的情绪，只能从这张表里选：{_EMOTION_VOCAB}。\n"
    "2. emotions 最多 2 个。第二个只在画面明确表现出时才加；不确定就只留一个。宁可少标，不许凑数。\n"
    "3. 禁止近义词堆叠：「无语/沉默/嫌弃」这类其实是一个判断，只保留最贴的那一个。\n"
    "4. tags 最多 3 个，只写看得见的画面元素（人物/动作/道具），不要写情绪、不要写风格套话、不要写用途。\n"
    "5. confidence 是你对 emotions 的把握，0~1。没把握就写 0.4 以下，不要硬猜。\n"
    "6. 只描述画面本身，禁止写「适合表达…」这类用途推测。\n"
)

# 检索权重：主情绪远高于其他字段。旧库没有主次之分时，emotions[0] 自动当主情绪。
_W_PRIMARY = 3.0      # emotions[0]，这张图最主要表达的情绪
_W_SECONDARY = 1.5    # emotions[1:] 次情绪
_W_TAGS = 1.2         # 画面关键词
_W_TAG_EXTRA = 0.45   # 同一张图覆盖了 query 的第 2、3 个画面词时的额外证据（封顶 2 个）
_W_INTENTS = 0.9      # 旧版 intents（新打标不再生成，仅为兼容老库保留）
_W_NAME = 0.5         # 名称（AI 起的名字最主观，权重必须低，旧版是 4.0）
_W_DESC = 0.5         # 画面描述
_W_VTEXT = 0.4        # 图上文字

# 自动发图的门槛：(弱命中最低分, 与第二名的领先倍数)。
# 关键取舍：只在 tags/name 里命中（旧库那种被 AI 乱塞的标签）单靠一次 tag 命中
# 拿不到 1.2 分以上，所以「适中」档要求至少达到次情绪级的证据才自动发图，
# 这一步专门挡住「一个错标签就把图发出去」。
# 用户要「每条都发」时门槛降到 0.85（tag 命中即可），不会变成一张都不发；
# 但无论哪档，score=0（毫无关系）永远不发。
_AUTO_GATE = {
    "off": (2.0, 1.50),
    "rare": (1.9, 1.50),
    "normal": (1.5, 1.35),
    "often": (1.0, 1.05),
    "always": (0.85, 1.00),
}
_STRONG_SCORE = 2.4   # 主情绪级命中，直接放行，不再要求领先倍数（同分时随机挑一张）

DEFAULT_BLOCKED_TAGS = "威胁,血腥,恐怖,暴力,持刀,自杀"

# 查询里的口水词：不参与匹配，否则「给我来一张表情包」会命中一堆图
_QUERY_STOPWORDS = frozenset(
    {
        "表情", "表情包", "图片", "一张", "一个", "来个", "来张", "发个", "发张",
        "给我", "想要", "这个", "那个", "一下", "有点", "比较", "可以", "什么",
        "怎么", "就是", "帮我", "配个", "张图", "个图",
    }
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


def _review_state(item: dict) -> str:
    """条目审核状态。老库没有这个字段 → 一律视为已通过。

    升级插件绝不能把别人已有的库变成「全部待审核」（那样等于图全发不出去了）。
    """
    r = str(item.get("review") or "").strip().lower()
    return r if r in ("pending", "rejected") else "approved"


# 来源徽章：管理页按天分组时显示
SOURCE_LABEL = {
    "upload": "手动添加",
    "import_dir": "批量导入",
    "steal": "偷的",
    "migrated_companion": "旧库迁移",
}


def _readable_name(s: str) -> str:
    """从 summary / file 字段里捞个能看的名字。

    QQ 传图片时 file 常是 32 位 hex 或 GUID，拿来当名字纯属垃圾；
    有中文名的（转发时保留的 "猫猫头.jpg"）才值得留。
    """
    base = str(s or "").strip()
    if not base:
        return ""
    base = base.rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    base = base.strip()
    base = base.strip("{}()[] ")
    if base.lower() in ("", "image", "pic", "图片", "表情"):
        return ""
    if re.fullmatch(r"[0-9a-zA-Z][0-9a-zA-Z\-_]{11,}", base):
        return ""  # 一长串字母数字 = 哈希 / GUID，不是人起的名字
    return base[:40]


def _sniff_image(raw: bytes) -> str:
    """按魔数认图片格式。imghdr 在 3.13 已移除，这里只认表情包会用到的几种。"""
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:2] == b"BM":
        return "image/bmp"
    return ""


_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


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


class _RateLimited(RuntimeError):
    """上游 429/503：服务端限流或过载，需要全局冷却而非普通重试。"""

    def __init__(self, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = retry_after


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
        self._inject_state: dict = {}
        self._brief_state: dict = {}
        self._last_tag_hit = 0.0
        self._last_pick: dict = {}
        # 打标队列：固定 worker 池 + 有界队列。以前是每张图一个 task，
        # 上传 100 张就瞬间建 100 个协程并各自抓一个信号量，重活全压在事件循环上。
        self._queue: asyncio.Queue = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._queued_ids: set[str] = set()
        self._active_ids: set[str] = set()
        self._queue_done = 0
        self._queue_failed = 0
        self._worker_count = 0
        # 暂停开关：暂停只拦"下一张"，正在飞的那几张会跑完（HTTP 请求没法安全中断，
        # 硬 cancel 会让那一条永远卡在 pending）。事件只活在内存里，重启插件即恢复运行。
        self._queue_paused = False
        self._resume_evt = asyncio.Event()
        self._resume_evt.set()
        self._queued_at: dict[str, float] = {}
        self._active_since: dict[str, float] = {}
        self._done_times: deque[float] = deque(maxlen=30)
        self._prov_cache: tuple[float, list[dict]] | None = None
        self._session: aiohttp.ClientSession | None = None
        # 按模型（base|model）记两件事，切候选模型时才不会互相牵连：
        # _effort_bad = 这个接口不认 reasoning_effort；_prov_down = 这个模型被限流到什么时候
        self._effort_bad: set[str] = set()
        self._prov_down: dict[str, float] = {}
        self._last_model = ""
        # 偷表情包：按会话缓存对方刚发的 QQ 表情（umo -> [{url, summary, at}]），
        # 只留几分钟，过期就当没发生（隔了半小时的「要偷吗」不该还算数）
        self._steal_pool: dict[str, list[dict]] = {}
        # 防抖用：umo -> 上一次成功偷那张时所在的消息 id。同一条消息里连偷几张
        # （用户一口气发三张说都要）走白名单，换一条消息才吃冷却。
        self._steal_last_msg: dict[str, str] = {}
        # 每日额度的缓存 (日期, 当时的库存条数, 已偷张数)，库存一变就重算
        self._steal_day_cache: tuple = ()
        self._dirty = False
        self._flush_task: asyncio.Task | None = None
        try:
            self._llm_min_interval = max(0.0, float(self.config.get("llm_min_interval", 1.0) or 0))
        except (TypeError, ValueError):
            self._llm_min_interval = 1.0
        self._llm_gate_lock = asyncio.Lock()
        self._llm_next_slot = 0.0
        self._llm_cooldown_until = 0.0
        self._register_panel()

    # ---------- 存储 ----------

    def _llm_conc(self) -> int:
        try:
            return max(1, min(8, int(self.config.get("llm_concurrency", 2) or 2)))
        except (TypeError, ValueError):
            return 2

    def _load_index(self) -> None:
        try:
            self._items = json.loads(self._index_path.read_text(encoding="utf-8"))
        except Exception:
            self._items = []
        # 上次被打断的 pending 不再抹成 none，而是留给 initialize() 重新排队，
        # 这样 100 张传到一半重启也不用一张张手动重打标。
        stuck = sum(1 for it in self._items if it.get("analysis_status") == "pending")
        if stuck:
            logger.info(f"[StickerKit] 发现 {stuck} 条上次被打断的打标任务，稍后自动续跑")

    def _save_index(self, immediate: bool = False) -> None:
        """写索引。批量打标时每张都全量重写会卡事件循环，非紧急情况合并成 1.5s 一次。"""
        self._emotion_cache = None  # 库存/情绪可能变了，分类统计失效
        if immediate:
            self._dirty = True
            self._flush_index()
            return
        self._dirty = True
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            self._flush_task = asyncio.ensure_future(self._flush_later())
        except RuntimeError:
            self._flush_index()

    async def _flush_later(self) -> None:
        try:
            await asyncio.sleep(1.5)
        finally:
            self._flush_index()

    def _flush_index(self) -> None:
        if not self._dirty and self._index_path.exists():
            return
        try:
            tmp = self._index_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._items, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self._index_path)
            self._vocab_cache = None  # 标签可能被改过，切词词典失效
            self._dirty = False
        except Exception as exc:
            logger.warning(f"[StickerKit] 写索引失败: {type(exc).__name__} {exc}")

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

    def _steal_denied(self, event: AstrMessageEvent) -> str:
        """偷表情包单独一道门：默认谁都能偷，开了开关才只认管理员。

        和 `admin_only` 分开是有意的 —— 别的工具是「发图/改库」，偷图只是往待审核里
        丢一张，让群友也能用才好玩；真怕被塞垃圾就把 `steal_admin_only` 打开。
        """
        if self.config.get("steal_admin_only", False) and not event.is_admin():
            return "权限拒绝：偷表情包只允许管理员触发（配置里开了「只允许管理员偷」）。"
        return self._steal_quota(event)

    def _steal_used_today(self) -> int:
        """今天已经偷了几张。

        直接数库里的记录（source=steal 且入库时间是今天），不另存状态文件：
        重启不会把额度清零；管理员手动删掉几张，额度也就还回来了，好解释。
        """
        day = time.strftime("%Y-%m-%d", time.localtime())
        cache = self._steal_day_cache
        if cache and cache[0] == day and cache[1] == len(self._items):
            return cache[2]
        n = 0
        for it in self._items:
            if str(it.get("source") or "") != "steal":
                continue
            try:
                ts = float(it.get("created_at") or 0)
            except (TypeError, ValueError):
                continue
            if ts and time.strftime("%Y-%m-%d", time.localtime(ts)) == day:
                n += 1
        self._steal_day_cache = (day, len(self._items), n)
        return n

    def _steal_cd_left(self, umo: str) -> float:
        """这个会话离下次能偷还剩多少秒（0 = 现在就能偷）。"""
        try:
            cd = float(self.config.get("steal_cooldown", 30) or 0)
        except (TypeError, ValueError):
            cd = 30.0
        if cd <= 0:
            return 0.0
        last = 0.0
        for it in self._items:
            if str(it.get("source") or "") != "steal":
                continue
            if str((it.get("stolen_from") or {}).get("session") or "") != umo:
                continue
            try:
                last = max(last, float(it.get("created_at") or 0))
            except (TypeError, ValueError):
                continue
        if not last:
            return 0.0
        return max(0.0, cd - (_now() - last))

    def _steal_quota(self, event) -> str:
        """冷却 / 每日上限。返回拒绝理由；可以偷时返回空串。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        mid = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        # 同一条消息里连着偷几张是正常操作，不算刷；换条消息才吃冷却
        if mid and self._steal_last_msg.get(umo) == mid:
            return ""
        try:
            limit = int(float(self.config.get("steal_daily_limit", 20) or 0))
        except (TypeError, ValueError):
            limit = 20
        if limit > 0:
            used = self._steal_used_today()
            if used >= limit:
                return (
                    f"今天偷表情的额度用完了（已偷 {used} 张，上限 {limit} 张），今天不再收录。"
                    "如实告诉用户明天再来，不要重试，也不要提议换个办法偷。"
                )
        left = self._steal_cd_left(umo)
        if left > 0:
            return (
                f"偷表情还在冷却（还剩 {left:.0f} 秒），这次不要收录。"
                "如实告诉用户先歇一会儿，不要重试。"
            )
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

    # ---------- 配图频率：档位 / 点名豁免 / 协议拼装 ----------

    def _frequency_level(self) -> str:
        raw = str(self.config.get("sticker_frequency", "适中") or "").strip()
        return _FREQ_ALIAS.get(raw.lower(), _FREQ_ALIAS.get(raw, "normal"))

    def _build_protocol_prompt(self) -> str:
        """按频档生成协议；custom_protocol_prompt 非空时整段替换内置文案。"""
        extra = str(self.config.get("protocol_prompt_extra") or "").strip()
        custom = str(self.config.get("custom_protocol_prompt") or "").strip()
        if not custom and self._frequency_level() == "off":
            self._brief_state = {}
            return ""
        brief = self._brief_render()
        self._brief_state = brief
        btext = str(brief.get("text") or "")
        if custom:
            if _PROTOCOL_MARK not in custom:
                custom = f"{_PROTOCOL_MARK} {custom}"
            head = f"{custom}\n{extra}" if extra else custom
        else:
            head = _protocol_body(self._frequency_level(), extra, with_vocab=not btext)
        return f"{head}\n{btext}" if btext else head

    # ---------- 库存概览：把分类压成一行拼进 system_prompt ----------
    #
    # system_prompt 不是"存"在模型那边的：LLM 接口无状态，每一轮都要把完整的
    # messages（含 system）重新发一遍、重新计费。所以这里给的是"每轮固定成本"，
    # 库存越大越贵，必须按预算截断而不是无脑全塞。

    def _brief_mode(self) -> str:
        raw = str(self.config.get("brief_mode", "自动") or "").strip()
        return {"自动": "auto", "手动": "manual", "关闭": "off"}.get(raw, "auto")

    def _brief_budget(self) -> int:
        try:
            v = int(float(self.config.get("brief_budget", 100) or 100))
        except (TypeError, ValueError):
            v = 100
        return max(20, min(600, v))

    def _brief_groups(self) -> list[str]:
        raw = str(self.config.get("brief_groups") or "")
        return [w.strip() for w in re.split(r"[,，、\s]+", raw) if w.strip()]

    def _emotion_counts(self) -> list[tuple[str, int, int]]:
        """[（标准词, 图数, 权重）]。权重让"主情绪"排在"次情绪"前面。

        别名先归一（委屈巴巴→委屈），同一张图里重复的情绪只算一次，
        否则库里 20 张「委屈」会被算成 50 次。
        """
        count: dict[str, int] = {}
        weight: dict[str, int] = {}
        cached = getattr(self, "_emotion_cache", None)
        if cached is not None:
            return cached
        for it in self._approved_items():
            if not it.get("enabled", True):
                continue
            raw = [str(e).strip() for e in (it.get("emotions") or []) if str(e).strip()]
            if not raw:
                continue
            seen: set[str] = set()
            for idx, e in enumerate(raw):
                canon = _ALIAS.get(e, e)
                if canon in ("其他", "其他情绪", "未标注", "未知"):
                    continue  # 这类分类没有任何检索价值，白占预算
                if canon in seen:
                    continue
                seen.add(canon)
                count[canon] = count.get(canon, 0) + 1
                weight[canon] = weight.get(canon, 0) + (2 if idx == 0 else 1)
        rows = [(w, count[w], weight[w]) for w in count]
        rows.sort(key=lambda r: (-r[2], -r[1], r[0]))
        self._emotion_cache = rows
        return rows

    def _brief_render(
        self,
        mode: str | None = None,
        budget: int | None = None,
        groups: list[str] | None = None,
    ) -> dict:
        """生成库存概览文本。

        返回 dict：
            text      实际会拼进 system_prompt 的那一行（可能为空 = 不注入）
            used      实际注入的估算 token
            all_used  勾选/自动选中的全部分类都塞进去需要的 token（用来看有没有超预算）
            picked    实际保留的分类词
            truncated 是否因为预算被砍掉了尾巴
        """
        mode = mode or self._brief_mode()
        budget = budget if budget else self._brief_budget()
        total = sum(1 for it in self._approved_items() if it.get("enabled", True))
        rows = self._emotion_counts()
        empty = {"text": "", "used": 0, "all_used": 0, "picked": [],
                 "truncated": False, "budget": budget, "total": total, "mode": mode}
        if mode == "off":
            return empty

        by_word = {r[0]: r for r in rows}
        if mode == "manual":
            want = self._brief_groups() if groups is None else groups
            chosen: list[tuple[str, int, int]] = []
            for w in want:
                row = by_word.get(w) or by_word.get(_ALIAS.get(w, w))
                if row and row not in chosen:
                    chosen.append(row)
        else:
            chosen = list(rows)
        if not chosen:
            return empty

        head = f"[表情库存{total}张] "
        all_used = _est_tokens(head + " ".join(f"{w}×{c}" for w, c, _ in chosen))
        used = _est_tokens(head)
        parts: list[str] = []
        truncated = False
        for word, cnt, _w in chosen:
            piece = f"{word}×{cnt}"
            cost = _est_tokens(piece) + 1  # +1 是分隔空格
            if used + cost > budget:
                truncated = True
                break
            parts.append(piece)
            used += cost
        if not parts:
            # 预算连一个分类都放不下：与其注入一行没信息量的标题，不如不注入
            return {**empty, "all_used": all_used, "truncated": bool(chosen), "picked": []}
        text = head + " ".join(parts)
        return {
            "text": text,
            "used": _est_tokens(text),
            "all_used": all_used,
            "picked": [w for w, _c, _wt in chosen if any(p.startswith(f"{w}×") for p in parts)],
            "truncated": truncated,
            "budget": budget,
            "total": total,
            "mode": mode,
        }

    def _named_keywords(self) -> list[str]:
        raw = self.config.get("named_request_keywords")
        if raw is None or str(raw).strip() == "":
            raw = DEFAULT_NAMED_KEYWORDS
        return [w.strip().lower() for w in re.split(r"[,，、]", str(raw)) if w.strip()]

    def _event_text(self, event: AstrMessageEvent) -> str:
        text = str(getattr(event, "message_str", "") or "")
        if not text:
            obj = getattr(event, "message_obj", None)
            text = str(getattr(obj, "message_str", "") or "")
        return text

    def _is_named_request(self, event: AstrMessageEvent) -> bool:
        """用户是否明确点名要表情包（用于跳过概率与冷却）。"""
        if not self.config.get("named_request_bypass", True):
            return False
        low = self._event_text(event).lower()
        if not low:
            return False
        return any(k in low for k in self._named_keywords())

    def _record_inject(self, total: int, level: str, skipped: str = "") -> None:
        self._inject_state = {
            "ts": _now(),
            "total": total,
            "level": level,
            "skipped": skipped,
        }

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

    def _tag_vocab(self) -> set[str]:
        """库存里现成的画面词，用来给「没打空格的长串」切词。

        词典来自库存本身，所以切出来的词一定有可能被命中，不会造出无意义片段。
        500 张库重建一次要 2ms 以上，按「对象身份 + 长度」缓存；索引落盘时清空。
        """
        stamp = (id(self._items), len(self._items))
        cache = getattr(self, "_vocab_cache", None)
        if cache is not None and cache[0] == stamp:
            return cache[1]
        vocab: set[str] = set()
        for it in self._approved_items():
            for t in it.get("tags") or []:
                s = str(t).strip()
                if 2 <= len(s) <= 5:
                    vocab.add(s)
        self._vocab_cache = (stamp, vocab)
        return vocab

    def _cn_split(self, text: str) -> list[str]:
        """无空格中文串按库存词表做正向最大匹配，认不出的字直接跳过。"""
        vocab = self._tag_vocab()
        out: list[str] = []
        i, n = 0, len(text)
        while i < n:
            for ln in (5, 4, 3, 2):
                seg = text[i : i + ln]
                if len(seg) == ln and seg in vocab:
                    out.append(seg)
                    i += ln
                    break
            else:
                i += 1
        return out

    def _keywords(self, query: str) -> list[str]:
        """按标点/空白切词并过滤口水词。

        不把中文拆成所有 2-gram（旧版那样「给我来一张表情包」会变成十几个人造词，
        任意一个撞上就 score>0，这是"检索乱成一团"的直接来源）；但完全不拆又会
        让「蓝发女仆眨眼」这种没打空格的长串退化成一个整词 —— 它跟每个单个 tag
        做包含匹配都只有 0.9，「眨眼」这个最有区分度的词被彻底平掉。折中做法：
        无空格长串只按库存词表切，且必须切出 ≥2 个词才采用，切不动就原样保留。
        """
        words = [w for w in re.split(r"[\s,，。.！!？?、/|]+", str(query or "").strip()) if w]
        words = [w for w in words if w not in _QUERY_STOPWORDS]
        split: list[str] = []
        for w in words:
            if len(w) >= 4 and not w.isascii():
                segs = self._cn_split(w)
                if len(segs) >= 2:
                    split.extend(segs)
                    continue
            split.append(w)
        words = split
        # 口语词补上标准词（呆萌→卖萌、心累→疲惫），否则「库存里有图却搜不到」
        extra: list[str] = []
        for w in words:
            canon = _ALIAS.get(w)
            if canon:
                extra.append(canon)
            elif len(w) > 2:
                for k, v in _ALIAS.items():
                    if len(k) >= 2 and k in w:
                        extra.append(v)
        return list(dict.fromkeys(words + extra))

    @staticmethod
    def _sim(a: str, b: str) -> float:
        """两个短词的相似度：编辑序列相似度与字符 Jaccard 取较大者。"""
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        r = difflib.SequenceMatcher(None, a, b).ratio()
        sa, sb = set(a), set(b)
        union = sa | sb
        j = len(sa & sb) / len(union) if union else 0.0
        return max(r, j)

    def _fuzzy_enabled(self) -> bool:
        return bool(self.config.get("fuzzy_search", True))

    @staticmethod
    def _quality(kw: str, token: str, fuzzy: bool) -> float:
        """单个查询词 vs 单个标签词的匹配质量，0~1。

        一条 token 只会返回一个质量值，上层再做 max —— 所以同一个词不会再被
        「精确 / 整句子串 / 模糊」三层各加一次分（旧版一个错标签能拿到 6.25 分）。
        """
        if not kw or not token:
            return 0.0
        if kw == token:
            return 1.0
        if _ALIAS.get(kw, kw) == _ALIAS.get(token, token):
            return 1.0  # 「呆萌」和「卖萌」、「沉默」和「无语」算同一个情绪
        if kw in token or token in kw:
            # 包含关系：双向都算，「委屈巴巴」的查询能命中「委屈」标签，
            # 反过来「委屈」标签也能被长句 query 命中。单字包含容易误伤，压权重。
            return 0.9 if min(len(kw), len(token)) >= 2 else 0.5
        if not fuzzy or len(kw) < 2 or len(token) < 2:
            return 0.0
        # 长度差超过 1 的词直接不比较，避免「委屈巴巴」被「委屈」之外的长词蹭到
        if abs(len(kw) - len(token)) > 1:
            return 0.0
        if not (set(kw) & set(token)):
            return 0.0  # 没有共同字，difflib 的 ratio 必然是 0，白算
        s = StickerKit._sim(kw, token)
        # 旧阈值 0.4 太松：「沉思/沉默」=0.5 会被判成同一个情绪。抬到 0.62。
        return s if s >= 0.62 else 0.0

    def _best_match(self, keywords: list[str], tokens: list[str], fuzzy: bool) -> float:
        best = 0.0
        for token in tokens:
            if not token:
                continue
            for kw in keywords:
                q = self._quality(kw, token, fuzzy)
                if q > best:
                    best = q
                    if best >= 1.0:
                        return 1.0
        return best

    def _match_detail(
        self, keywords: list[str], tokens: list[str], fuzzy: bool
    ) -> tuple[float, int]:
        """一次遍历同时得到「最佳命中质量」和「有多少个查询词被覆盖」。

        分成两趟算（best 一趟、covered 一趟）会让 500 张库的检索翻倍，
        而这两件事本来就要把 keywords × tokens 走一遍。
        """
        best = 0.0
        covered = 0
        for kw in keywords:
            hit = 0.0
            for token in tokens:
                if not token:
                    continue
                q = self._quality(kw, token, fuzzy)
                if q > hit:
                    hit = q
                    if hit >= 1.0:
                        break
            if hit > best:
                best = hit
            if hit >= 0.85:
                covered += 1
        return best, covered

    def _score_item(self, keywords: list[str], raw_query: str, item: dict) -> float:
        """按字段取「最佳命中」后加权求和。

        三个要点：
        1. 每字段取 max，不累加 —— 标签多不再等于更容易被选中（旧版是加法，6 个标签的图有 6 次机会）。
        2. 主情绪（emotions[0]）权重最高，名称权重从 4.0 降到 0.5。
        3. 同一 token 只算一次，不存在精确/模糊重复计分。
        """
        if not item.get("enabled", True):
            return -1.0
        fuzzy = self._fuzzy_enabled()
        emos = [str(e).strip() for e in (item.get("emotions") or []) if str(e).strip()]
        tags = [str(t).strip() for t in (item.get("tags") or []) if str(t).strip()]
        intents = [str(t).strip() for t in (item.get("intents") or []) if str(t).strip()]
        score = 0.0
        if emos:
            score += _W_PRIMARY * self._best_match(keywords, emos[:1], fuzzy)
            if len(emos) > 1:
                score += _W_SECONDARY * self._best_match(keywords, emos[1:], fuzzy)
        if tags:
            best_tag, covered = self._match_detail(keywords, tags, fuzzy)
            score += _W_TAGS * best_tag
            # 一张图同时覆盖 query 的多个画面词时，证据强度确实更高（「蓝发」+「眨眼」
            # 都中，比只中「蓝发」可信得多）。注意是按 keywords 计数、不是按 tags 计数：
            # 图里标签多不会自动加分，必须是 query 的多个词被真正覆盖，所以不会回到
            # 旧版"标签越多越容易被选中"。封顶 2 个，最多 +0.9，压不过主情绪 3.0。
            score += _W_TAG_EXTRA * min(max(0, covered - 1), 2)
        if intents:
            score += _W_INTENTS * self._best_match(keywords, intents, fuzzy)
        name = str(item.get("name") or "").strip()
        if name:
            score += _W_NAME * self._best_match(keywords, [name], fuzzy)
        desc = str(item.get("description") or "").strip()
        if desc:
            score += _W_DESC * self._best_match(keywords, [desc], fuzzy)
        vtext = str(item.get("visible_text") or "").strip()
        if vtext:
            score += _W_VTEXT * self._best_match(keywords, [vtext], fuzzy)
        return score

    def _approved_items(self) -> list[dict]:
        """能参与检索与发送的条目：待审核/已拒绝的偷来图不算。"""
        return [it for it in self._items if _review_state(it) == "approved"]

    def _search(self, query: str, top_k: int, min_score: float = 0.0) -> list[tuple[float, dict]]:
        keywords = self._keywords(query)
        if not keywords:
            return []
        scored = [(self._score_item(keywords, query, it), it) for it in self._approved_items()]
        scored = [(s, it) for s, it in scored if s > max(0.0, min_score - 1e-6)]
        scored.sort(key=lambda pair: -pair[0])
        return scored[: max(1, int(top_k or 6))]

    # ---------- 自动配图闸门 ----------

    def _blocked_words(self) -> list[str]:
        raw = self.config.get("blocked_tags")
        if raw is None or str(raw).strip() == "":
            raw = DEFAULT_BLOCKED_TAGS
        return [w.strip() for w in re.split(r"[,，、\s]+", str(raw)) if w.strip()]

    def _blocked_hit(self, item: dict) -> str:
        words = self._blocked_words()
        if not words:
            return ""
        for t in list(item.get("emotions") or []) + list(item.get("tags") or []):
            s = str(t)
            for w in words:
                if w and w in s:
                    return w
        return ""

    def _search_detail(self) -> str:
        """检索结果详细程度：精简 / 标准 / 完整。控制工具返回的长度。"""
        mode = str(self.config.get("search_detail") or "标准").strip()
        return mode if mode in ("精简", "标准", "完整") else "标准"

    def _gate_thresholds(self) -> tuple[float, float]:
        """弱命中门槛与领先倍数：用户配的优先，否则按配图频档自动放宽。"""
        auto_min, auto_margin = _AUTO_GATE.get(self._frequency_level(), _AUTO_GATE["normal"])
        try:
            cfg_min = float(self.config.get("min_score", 0) or 0)
        except (TypeError, ValueError):
            cfg_min = 0.0
        try:
            cfg_margin = float(self.config.get("min_margin", 0) or 0)
        except (TypeError, ValueError):
            cfg_margin = 0.0
        min_score = cfg_min if cfg_min > 0 else auto_min
        margin = cfg_margin if cfg_margin > 0 else auto_margin
        return max(0.1, min_score), max(1.0, margin)

    def pick_for_query(self, query: str) -> tuple[dict | None, dict]:
        """快速通道选图：返回 (item, 诊断信息)。item 为 None 表示这次不该发图。

        判定顺序：分数为 0 / 只命中危险标签 / 低于门槛 / 领先不足 —— 任何一条不过就只发文字。
        """
        info = {"query": str(query or ""), "reason": "", "score": 0.0, "second": 0.0}
        q = str(query or "").strip()
        if not q:
            info["reason"] = "空查询"
            return None, info
        hits = self._search(q, 6)
        if not hits:
            info["reason"] = "无任何匹配"
            return None, info
        # 先把命中危险标签的候选踢出候选池（AI 的检索词本身提到它时才算用户真实意图）
        usable = [
            (s, it)
            for s, it in hits
            if not (self._blocked_hit(it) and self._blocked_hit(it) not in q)
        ]
        dropped = len(hits) - len(usable)
        if dropped:
            info["blocked"] = dropped
        if not usable:
            info["reason"] = "候选全部命中危险标签"
            return None, info
        top_score, top_item = usable[0]
        second_score = usable[1][0] if len(usable) > 1 else 0.0
        info.update(score=round(top_score, 2), second=round(second_score, 2), name=top_item.get("name"))
        min_score, margin = self._gate_thresholds()
        info["min_score"] = round(min_score, 2)
        if top_score >= _STRONG_SCORE:
            # 强命中（主情绪级）：同分的近似候选随机挑一张，避免永远发同一张。
            floor = max(_STRONG_SCORE, top_score * 0.9)
            pool = [it for s, it in usable if s >= floor]
            return (random.choice(pool) if pool else top_item), info
        if top_score < min_score:
            info["reason"] = f"分数 {top_score:.2f} 低于门槛 {min_score:.2f}"
            return None, info
        if second_score > 0 and abs(top_score - second_score) < 1e-6:
            # 分数完全并列 = 几张图同样贴切，随机挑一张；并列不等于"分不清"，
            # 不能因为并列就一张都不发（旧库里很多图把同一个情绪放在次情绪位）。
            tied = [it for s, it in usable if abs(s - top_score) < 1e-6]
            return random.choice(tied), info
        if second_score > 0 and top_score < second_score * margin:
            info["reason"] = f"领先不足（{top_score:.2f} vs {second_score:.2f}）"
            return None, info
        return top_item, info

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="sticker_search")
    async def sticker_search(self, event: AstrMessageEvent, query: str):
        """按对话语境检索表情包，返回最匹配的候选列表（含 sticker_id 和描述）。

        优先用快速通道：直接在回复末尾输出 <sticker>{"query":"关键词"}</sticker> 即可自动配图，无需调用本工具。
        本工具仅在需要精确挑选（检索后用 sticker_pick 指定发送）时调用；没有合适的就直接文字回复，不要硬选。
        Args:
            query(string): 检索需求，写清情绪/场景/内容，如「得意 自夸」「委屈」
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
        mode = self._search_detail()
        desc_chars = {"精简": 0, "标准": 36, "完整": 60}.get(mode, 36)
        # 工具返回会永久留在对话历史里，每轮都要重新计费。所以按档位裁剪：
        # 精简只给「id + 名称 + 主情绪」，AI 够用来挑；要看画面细节再切标准/完整。
        lines = [f"共 {len(hits)} 个候选，选一个最贴语境的，用 sticker_pick(sticker_id, caption) 发送："]
        for rank, (score, it) in enumerate(hits, 1):
            emos = [str(e) for e in (it.get("emotions") or [])]
            primary = emos[0] if emos else "未标注"
            warn = " ⚠️危险标签" if self._blocked_hit(it) else ""
            line = (
                f"#{rank} sticker_id={it['id']} 名称={it.get('name')} "
                f"主情绪={primary} 匹配分={score:.1f}{warn}"
            )
            if mode == "完整":
                more = "/".join(emos[1:3])
                if more:
                    line += f"（次:{more}）"
                conf = (it.get("analysis_meta") or {}).get("confidence")
                if isinstance(conf, (int, float)):
                    line += f" 置信{float(conf):.2f}"
            lines.append(line)
            if desc_chars:
                desc = str(it.get("description") or "")[:desc_chars]
                if desc:
                    lines.append(f"    {desc}")
        lines.append("主情绪是这张图最主要的表达；匹配分低于 1 的不要选。")
        lines.append("发送时 caption 必须是一条完整、自然、可独立成立的正文；图片会按投递设置跟在正文后（可能单独一条消息）。")
        return "\n".join(lines)

    @filter.llm_tool(name="sticker_pick")
    async def sticker_pick(self, event: AstrMessageEvent, sticker_id: str, caption: str):
        """发送指定的表情包图片。必须先用 sticker_search 拿到 sticker_id，禁止凭空填写。

        更省事的做法是走快速通道：不调用本工具，直接在回复末尾输出
        <sticker>{"query":"关键词"}</sticker>，系统会自动选图并追加到正文后。
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
        if not self._is_named_request(event):
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
        mode = str(self.config.get("delivery_mode") or "separate_after").lower()
        if mode not in {"separate_after", "same_message"}:
            mode = "separate_after"
        try:
            if mode == "same_message":
                result = await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain([Plain(caption), image_component]),
                )
                if result is False:
                    return "发送失败：平台返回 False，请如实告知用户发送未成功。"
                delivered = "同一条消息"
            else:
                text_result = await self.context.send_message(
                    event.unified_msg_origin, MessageChain([Plain(caption)])
                )
                if text_result is False:
                    return "发送失败：文字部分未发出（平台返回 False），请如实告知用户本次未发送成功。"
                try:
                    gap_ms = int(float(self.config.get("separate_send_gap_ms", 350) or 0))
                except (TypeError, ValueError):
                    gap_ms = 350
                if gap_ms > 0:
                    await asyncio.sleep(min(gap_ms, 3000) / 1000)
                img_result = await self.context.send_message(
                    event.unified_msg_origin, MessageChain([image_component])
                )
                if img_result is False:
                    self._mark_sent(event, item)
                    self._engage_lock()
                    return "文字已发出，但表情包图片发送失败（平台返回 False），请如实告知用户图没发出去。"
                delivered = "文字 + 表情包两条消息"
        except Exception as exc:
            return f"发送失败：{exc}"
        self._mark_sent(event, item)
        self._engage_lock()
        return (
            f"表情包「{item.get('name')}」已作为{delivered}发送成功。"
            "正文已经发出，本轮不要再重复相同内容，也不要再调用任何表情包工具验证。"
        )

    @filter.llm_tool(name="sticker_list")
    async def sticker_list(self, event: AstrMessageEvent, page: int = 1):
        """浏览表情包库存列表（名称+描述摘要）。

        仅管理员可用；仅在主人想了解表情包库存时调用。
        Args:
            page(number): 页码，从 1 开始，每页条数由 list_page_size 决定
        """
        denied = self._admin_check(event)
        if denied:
            return denied
        blocked = self._feature_check("search")
        if blocked:
            return blocked
        items = [it for it in self._approved_items() if it.get("enabled", True)]
        if not items:
            return "表情包库存为空，请管理员在面板上传。"
        page = max(1, int(page or 1))
        try:
            per = max(1, int(self.config.get("list_page_size", 10) or 10))
        except (TypeError, ValueError):
            per = 10
        chunk = items[(page - 1) * per : page * per]
        if not chunk:
            return f"第 {page} 页为空（共 {len(items)} 张，{(len(items) + per - 1) // per} 页）。"
        lines = [f"表情包库存（第 {page} 页，共 {len(items)} 张，每页 {per} 条）："]
        desc_chars = {"精简": 0, "标准": 24, "完整": 40}.get(self._search_detail(), 24)
        for it in chunk:
            emo = "/".join((it.get("emotions") or [])[:3])
            row = f"- {it.get('name')} | 情绪={emo}"
            if desc_chars:
                desc = str(it.get("description") or "")[:desc_chars]
                if desc:
                    row += f" | {desc}"
            lines.append(row)
        return "\n".join(lines)

    @filter.llm_tool(name="sticker_steal")
    async def sticker_steal(self, event: AstrMessageEvent, index: int = 0):
        """把对方刚发的表情包收进表情包库。

        QQ 表情（商城表情）和当图片发出来的收藏表情/表情图都算，都在这 5 分钟内的
        候选里。只在用户明确表示想收藏/想收下/想偷这张表情时才调用；用户没提就不要调用，
        更不要自己决定「这张不错我替他收了」。调用前别先说「看我把这张偷回来」，
        等工具返回结果再说 —— 链接可能过期、图可能太大，提前宣布会下不来台。
        Args:
            index(number): 对方一次发了好几张时用来挑，0 = 最新一张（默认）
        """
        denied = self._steal_denied(event)
        if denied:
            return denied
        if not self._steal_on():
            return "偷表情包功能没开（配置里关着），不要收录，也不要主动提起。"
        if not self.config.get("enable_send", True):
            return "表情包功能整体未启用，无法收录。"
        try:
            return await self._steal_one(event, index)
        except Exception as exc:
            logger.warning(f"[StickerKit] 偷表情包失败: {type(exc).__name__} {exc}")
            return f"收录失败（{type(exc).__name__}: {exc}），如实告诉用户没成功即可。"

    def _prune_tools(self, req: ProviderRequest) -> None:
        """把关掉的功能对应的工具从这次请求里摘掉。

        AstrBot 每次请求都会把所有工具的说明一起发出去，四个工具加起来一千来字；
        功能都关着还挂着说明纯属白烧 token。只替换本次请求的 ToolSet，不碰全局。
        """
        ts = getattr(req, "func_tool", None)
        tools = getattr(ts, "tools", None)
        if ts is None or not tools:
            return
        drop: set[str] = set()
        if not self._steal_on():
            drop.add("sticker_steal")
        if not self.config.get("enable_search", True):
            drop.update(("sticker_search", "sticker_list"))
        if not self.config.get("enable_send", True):
            drop.add("sticker_pick")
        if not drop:
            return
        keep = [t for t in tools if getattr(t, "name", "") not in drop]
        if len(keep) == len(tools):
            return
        try:
            req.func_tool = type(ts)(tools=keep)
        except Exception as exc:
            logger.debug(f"[StickerKit] 裁剪工具集失败: {type(exc).__name__} {exc}")

    # ---------- 快速通道：协议标签配图 ----------

    @filter.on_llm_request(priority=-99999)
    async def inject_sticker_protocol(self, event: AstrMessageEvent, req: ProviderRequest):
        """把表情标签协议并入 system_prompt（幂等 + 最后写入）。

        priority 取极小值：AstrBot 按 priority 从大到小执行钩子，越小越晚执行。
        设成 -99999 可确保本插件在最后一步写入，不会被 priority=0 附近、
        尤其不会被「整体赋值 req.system_prompt = ...」的插件覆盖。
        同时做幂等检查，避免同一请求内被重复注入。
        """
        # 偷表情包的提示先处理，且不受 protocol_mode / 配图频档影响：
        # 关掉自动配图的人照样可能想收别人的表情包
        try:
            steal_hint = self._steal_hint(event)
        except Exception as exc:
            logger.debug(f"[StickerKit] 生成偷表情提示失败: {type(exc).__name__} {exc}")
            steal_hint = ""
        if steal_hint:
            try:
                cur = str(getattr(req, "system_prompt", "") or "")
                if _STEAL_MARK not in cur:
                    req.system_prompt = f"{cur}\n{steal_hint}" if cur.strip() else steal_hint
            except Exception:
                pass
        try:
            self._prune_tools(req)
        except Exception as exc:
            logger.debug(f"[StickerKit] 裁剪工具集失败: {type(exc).__name__} {exc}")
        if not self.config.get("protocol_mode", True):
            return
        if not self.config.get("enable_send", True):
            return
        if self.config.get("admin_only", True) and not event.is_admin():
            return
        try:
            prompt = self._build_protocol_prompt()
        except Exception as exc:
            logger.warning(f"[StickerKit] 构建表情协议失败: {type(exc).__name__} {exc}")
            return
        level = self._frequency_level()
        try:
            current = str(getattr(req, "system_prompt", "") or "")
            if not prompt:
                self._record_inject(len(current), level, f"频档={_FREQ_LABELS.get(level, level)}，未注入")
                return
            if _PROTOCOL_MARK in current:
                self._record_inject(len(current), level, "已存在协议，跳过重复注入")
                return
            req.system_prompt = f"{current}\n{prompt}" if current.strip() else prompt
            merged = str(getattr(req, "system_prompt", "") or "")
            if _PROTOCOL_MARK not in merged:
                logger.warning("[StickerKit] 注入后未检测到协议标记：可能有插件覆盖了 system_prompt")
                self._record_inject(len(merged), level, "注入未生效（疑似被覆盖）")
                return
            if self.config.get("inject_to_user_prompt", False):
                user = str(getattr(req, "prompt", "") or "")
                hint = '（要配表情包时，请在回复末尾另起一行输出 <sticker>{"query":"关键词"}</sticker>）'
                if "<sticker>" not in user:
                    req.prompt = f"{user}\n{hint}" if user.strip() else hint
            self._record_inject(len(merged), level)
            logger.info(
                f"[StickerKit] 表情协议已注入：频档={_FREQ_LABELS.get(level, level)} "
                f"协议{len(prompt)}字 总长{len(merged)}"
            )
        except Exception as exc:
            logger.warning(f"[StickerKit] 注入表情协议失败: {type(exc).__name__} {exc}")

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
        self._last_tag_hit = _now()
        send = self.config.get("enable_send", True)
        if send and self.config.get("admin_only", True) and not event.is_admin():
            send = False
            logger.info("[StickerKit] 快速通道跳过(静默拦截已记录)：非管理员消息")
        named = bool(send) and self._is_named_request(event)
        if named and send:
            logger.info("[StickerKit] 用户点名要表情包：跳过概率与冷却")
        if send and not named and self._cooldown_hit(event):
            remain = float(self.config.get("send_cooldown_seconds", 30) or 0) - (_now() - self._last_send.get(event.unified_msg_origin, 0))
            send = False
            logger.info(f"[StickerKit] 快速通道跳过：会话冷却中(剩 {remain:.0f}s) session={event.unified_msg_origin[:60]}")
        if send and not named and not self._probability_hit():
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
            item, pick_info = self.pick_for_query(query)
            self._last_pick = {**pick_info, "ts": _now(), "session": event.unified_msg_origin[:40]}
            if item is None:
                logger.info(
                    f"[StickerKit] 快速通道不发图（query={query[:40]!r}）：{pick_info.get('reason') or '无匹配'}"
                )
            else:
                logger.info(
                    f"[StickerKit] 快速通道选图：{item.get('name')} "
                    f"score={pick_info.get('score')} second={pick_info.get('second')} query={query[:40]!r}"
                )
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
            pending_review = sum(1 for it in self._items if _review_state(it) == "pending")
            analyzing = sum(1 for it in self._items if it.get("analysis_status") == "pending")
            level = self._frequency_level()
            label = _FREQ_LABELS.get(level, level)
            inj = self._inject_state or {}
            if inj.get("ts"):
                inj_txt = f"{_now() - inj['ts']:.0f} 秒前 · 总长 {inj.get('total', 0)} 字"
                if inj.get("skipped"):
                    inj_txt += f" · {inj['skipped']}"
            else:
                inj_txt = "本进程尚未触发（还没走过 LLM 请求）"
            hit_txt = f"{_now() - self._last_tag_hit:.0f} 秒前" if self._last_tag_hit else "无"
            brief = self._brief_state or {}
            b_mode = {"auto": "自动", "manual": "手动", "off": "关闭"}.get(
                str(brief.get("mode") or ""), "未启用"
            )
            if brief.get("text"):
                b_txt = f"{b_mode} · 约 {brief.get('used', 0)}/{brief.get('budget', 0)} token · {len(brief.get('picked') or [])} 个分类"
                if brief.get("truncated"):
                    b_txt += " · 已按预算截断"
            else:
                b_txt = f"{b_mode} · 未注入"
            q = self._queue_stats()
            q_txt = (
                f"进行 {q['active']} / 排队 {q['queued']} / 剩余未完成 {q['pending']} / "
                f"失败 {q['failed']} / 并发 {q['workers']}"
            )
            if q["cooldown"] > 0:
                q_txt += f"｜上游冷却中 {q['cooldown']:.0f}s"
            if q["paused"]:
                q_txt += "｜已暂停"
            elif q["eta"] > 0:
                q_txt += f"｜预计还需 {int(q['eta'])}s"
            pick = self._last_pick or {}
            if pick.get("ts"):
                if pick.get("name"):
                    pick_txt = f"{pick['name']}（分 {pick.get('score')}）"
                else:
                    pick_txt = f"未发图 · {pick.get('reason') or '无匹配'}"
                pick_txt = f"{_now() - pick['ts']:.0f} 秒前 · {pick_txt} · query={str(pick.get('query'))[:20]}"
            else:
                pick_txt = "无"
            review_txt = f"，待审核 {pending_review} 张" if pending_review else ""
            steal_txt = "，偷表情包已开" if self._steal_on() else ""
            yield event.plain_result(
                f"StickerKit v1.10.3：库存 {total} 张{review_txt}"
                f"（本次启动迁移自陪伴插件 {self._migrated} 张），打标中 {analyzing}{steal_txt}\n"
                f"打标模型：{self._llm_source_desc()}\n"
                f"配图频率：档位={label} · 概率={float(self.config.get('send_probability', 1.0)):.0%}"
                f" · 冷却={int(float(self.config.get('send_cooldown_seconds', 30) or 0))}s"
                f" · 点名豁免={'开' if self.config.get('named_request_bypass', True) else '关'}\n"
                f"快速通道={'开' if self.config.get('protocol_mode', True) else '关'}"
                f"，投递={self.config.get('delivery_mode', 'separate_after')}"
                f"，格式={self.config.get('image_format', 'image')}\n"
                f"协议注入：{inj_txt}｜上次标签命中：{hit_txt}\n"
                f"库存概览：{b_txt}\n"
                f"打标队列：{q_txt}\n"
                f"上次选图：{pick_txt}\n"
                f"面板地址：WebUI → /api/plug/{PANEL_PREFIX}"
            )
        elif sub == "freq":
            parts = msg.split()
            valid = ("关闭", "很少", "适中", "较多", "每条都发")
            if len(parts) < 3:
                yield event.plain_result(
                    f"当前配图档位：{_FREQ_LABELS.get(self._frequency_level(), '适中')}。"
                    f"用法：/sticker freq {'|'.join(valid)}"
                )
                return
            val = parts[2].strip()
            if val not in valid:
                yield event.plain_result(f"档位只接受：{' / '.join(valid)}")
                return
            try:
                self.config["sticker_frequency"] = val
            except Exception:
                yield event.plain_result("写入配置失败：配置对象不可写，请到面板修改。")
                return
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                try:
                    saver()
                    mode = "已保存"
                except Exception:
                    mode = self._write_cfg_file()
            else:
                mode = self._write_cfg_file()
            yield event.plain_result(f"配图档位已设为「{val}」（{mode}），下一轮对话生效。")
        elif sub == "reload":
            self._load_index()
            yield event.plain_result(f"索引已重载：{len(self._items)} 条")

    # ---------- 打标 ----------

    # ---------- 复用 AstrBot 已配置的模型提供商 ----------
    # 打标仍然是插件自己发 HTTP 请求（这样 reasoning_effort / 超时 / 并发都还能控），
    # 只是把 api_base / key / model 从 AstrBot 的提供商配置里读出来，省得用户填两遍。

    # 这些类型的接口不是 OpenAI 的 /chat/completions 形状，拿来打标会直接 404/400
    _PROVIDER_SKIP = ("anthropic", "claude", "gemini", "google", "bedrock", "vertex")

    def _provider_candidates(self) -> list[dict]:
        """AstrBot 里可用的 OpenAI 兼容聊天提供商。`_key` 是内部字段，不要直接吐给前端。"""
        now = _now()
        cached = self._prov_cache
        if cached and now - cached[0] < 5.0:
            return cached[1]
        out: list[dict] = []
        try:
            provs = self.context.get_all_providers() or []
        except Exception as exc:
            logger.debug(f"[StickerKit] 读取 AstrBot 提供商失败: {exc}")
            provs = []
        for p in provs:
            try:
                cfg = dict(getattr(p, "provider_config", {}) or {})
                ptype = str(cfg.get("type") or "")
                if any(t in ptype.lower() for t in self._PROVIDER_SKIP):
                    continue
                base = str(cfg.get("api_base") or cfg.get("base_url") or "").strip().rstrip("/")
                try:
                    keys = list(p.get_keys())
                except Exception:
                    keys = list(cfg.get("key") or [])
                key = next((str(k).strip() for k in keys if str(k).strip()), "")
                model = ""
                try:
                    model = str(p.get_model() or "").strip()
                except Exception:
                    pass
                if not model:
                    mc = cfg.get("model_config") or {}
                    if isinstance(mc, dict):
                        model = str(mc.get("model") or "").strip()
                if not (base and key and model):
                    continue
                out.append({
                    "id": str(cfg.get("id") or ""),
                    "model": model,
                    "type": ptype,
                    "base": base,
                    "_key": key,
                })
            except Exception:
                continue
        self._prov_cache = (now, out)
        return out

    def _provider_default_id(self) -> str:
        try:
            return str(getattr(self.context.provider_manager, "default_chat_provider_id", "") or "")
        except Exception:
            return ""

    def _provider_conf(self) -> tuple[str, str, str]:
        cands = self._provider_candidates()
        if not cands:
            return ("", "", "")
        want = str(self.config.get("llm_provider_id") or "").strip()
        pick = next((c for c in cands if c["id"] == want), None) if want else None
        if pick is None:
            dft = self._provider_default_id()
            pick = next((c for c in cands if c["id"] == dft), None) or cands[0]
        return (pick["base"], pick["_key"], pick["model"])

    def _provider_public(self) -> list[dict]:
        return [
            {"id": c["id"], "model": c["model"], "type": c["type"], "base": c["base"]}
            for c in self._provider_candidates()
        ]

    def _llm_source(self) -> str:
        src = str(self.config.get("llm_source") or "自动").strip()
        if src in ("跟随 AstrBot", "跟随AstrBot", "astrbot", "provider"):
            return "astrbot"
        if src in ("手动填写", "手动", "manual"):
            return "manual"
        return "auto"

    def _llm_conf(self) -> tuple[str, str, str]:
        base = str(self.config.get("llm_api_base") or "").strip().rstrip("/")
        key = str(self.config.get("llm_api_key") or "").strip()
        model = str(self.config.get("llm_model") or "").strip()
        src = self._llm_source()
        if src == "manual":
            return base, key, model
        pb, pk, pm = self._provider_conf()
        if src == "astrbot":
            # 明确指定跟随 AstrBot：手填的三项只当兜底（比如 provider 被删了）
            return (pb or base, pk or key, pm or model)
        # auto：手填的齐全就用手填（不破坏老用户），缺哪项用 AstrBot 的补上
        if base and key and model:
            return base, key, model
        return (pb or base, pk or key, pm or model)

    def _provider_pool_ids(self) -> list[str]:
        """候选打标模型（配置里是逗号分隔的 provider id，顺序即优先级）。"""
        raw = self.config.get("llm_provider_pool") or ""
        if isinstance(raw, (list, tuple)):
            parts = [str(x) for x in raw]
        else:
            parts = re.split(r"[,，;；\s]+", str(raw))
        out: list[str] = []
        for p in parts:
            p = p.strip()
            if p and p not in out:
                out.append(p)
        return out

    @staticmethod
    def _tkey(base: str, model: str) -> str:
        return f"{base}|{model}"

    def _llm_targets(self) -> list[dict]:
        """实际可用的打标目标：主模型在前，候选池按配置顺序去重追加。

        每项 {base, key, model, label, pk}；pk 用于按模型记熔断状态。
        """
        out: list[dict] = []
        seen: set[str] = set()

        def add(base: str, key: str, model: str, label: str) -> None:
            base = str(base or "").strip().rstrip("/")
            key = str(key or "").strip()
            model = str(model or "").strip()
            if not (base and key and model):
                return
            pk = self._tkey(base, model)
            if pk in seen:
                return
            seen.add(pk)
            host = re.sub(r"^https?://", "", base).split("/")[0]
            out.append({
                "base": base,
                "key": key,
                "model": model,
                "label": label or f"{model} @ {host}",
                "pk": pk,
            })

        # 主模型：手填 / AstrBot 提供商按 llm_source 决定，就是原来那套
        base, key, model = self._llm_conf()
        add(base, key, model, "")

        # 候选：只认还在 AstrBot 里的 provider，被删掉的静默跳过
        pool = self._provider_pool_ids()
        if pool:
            cands = {c["id"]: c for c in self._provider_candidates() if c.get("id")}
            for pid in pool:
                c = cands.get(pid)
                if c:
                    add(c["base"], c["_key"], c["model"], f'{c["model"]} — {c["id"]}')
        return out

    def _targets_ready(self) -> list[dict]:
        """按「没被限流」优先排序，被限流的挪到最后（真没别的可用时还得试它）。"""
        targets = self._llm_targets()
        if not targets:
            return []
        now = _now()
        up = [t for t in targets if self._prov_down.get(t["pk"], 0.0) <= now]
        down = sorted(
            (t for t in targets if self._prov_down.get(t["pk"], 0.0) > now),
            key=lambda t: self._prov_down.get(t["pk"], 0.0),
        )
        return up + down

    def _llm_source_desc(self) -> str:
        """给人看的一行说明，配置页和 /sticker status 都能用。"""
        src = self._llm_source()
        targets = self._llm_targets()
        if not targets:
            return "未配置（手填 API，或在配置里选一个 AstrBot 提供商）"
        tag = {"manual": "手填", "astrbot": "跟随 AstrBot"}.get(src, "自动")
        main = targets[0]
        host = re.sub(r"^https?://", "", main["base"]).split("/")[0]
        tail = f"（+{len(targets) - 1} 个候选）" if len(targets) > 1 else ""
        return f"{tag} · {main['model']} @ {host}{tail}"

    def _llm_ready(self) -> bool:
        # 主模型没配但候选里有能用的，也算就绪
        return bool(self._llm_targets())

    def _http(self) -> aiohttp.ClientSession:
        """复用一个 ClientSession。旧版每张图都新建 session，等于每张都重做一次
        TCP + TLS 握手，批量打标时这部分开销能占到三成。"""
        cur = self._session
        if cur is None or cur.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120, sock_read=90),
                connector=aiohttp.TCPConnector(limit=0, ttl_dns_cache=300),
            )
        return self._session

    @staticmethod
    def _prepare_image(raw: bytes, mime: str, max_side: int) -> tuple[bytes, str]:
        """打标前把图缩到 max_side 以内。

        视觉模型不需要 2000px 原图，但 base64 体积是按像素平方涨的：
        一张 2MB 的 PNG 编码完接近 2.7MB，上传耗时远大于模型推理本身。
        """
        if max_side <= 0 or _PILImage is None:
            return raw, mime
        try:
            im = _PILImage.open(io.BytesIO(raw))
            try:
                im.seek(0)  # GIF 只取第一帧
            except Exception:
                pass
            if max(im.size) <= max_side and (im.format or "").upper() in ("JPEG", "PNG"):
                return raw, mime
            im = im.convert("RGB")
            w, h = im.size
            if max(w, h) > max_side:
                ratio = max_side / float(max(w, h))
                im = im.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), _RESAMPLE)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82, optimize=True)
            return buf.getvalue(), "image/jpeg"
        except Exception:
            return raw, mime

    @staticmethod
    def _norm_conf(v) -> float | None:
        try:
            c = float(v)
        except (TypeError, ValueError):
            return None
        if c > 1.0:
            c = c / 100.0
        return max(0.0, min(1.0, c))

    async def _llm_gate(self) -> None:
        """全局节流闸：相邻两次请求的发出间隔 >= llm_min_interval，
        429/503 冷却期内整个队列一起等。持锁等待让请求在锁上自然排队。"""
        async with self._llm_gate_lock:
            while True:
                delay = max(self._llm_next_slot, self._llm_cooldown_until) - _now()
                if delay <= 0:
                    break
                await asyncio.sleep(min(delay, 3.0))
            self._llm_next_slot = _now() + self._llm_min_interval

    async def _analyze_item(self, item: dict, force: bool = False) -> None:
        """按「主模型 → 候选模型」的顺序试，谁先成谁算。

        图片读取、缩放、base64 只做一次，换模型只换请求头里的 base/key/model，
        所以多配几个候选不会让单张变慢，只在上游限流/故障时才多花一次往返。
        """
        targets = self._targets_ready()
        if not targets:
            item["analysis_status"] = "none"
            return
        path = self._img_dir / item["stored_name"]
        try:
            raw = await asyncio.to_thread(path.read_bytes)
        except Exception as exc:
            item["analysis_status"] = "failed"
            item["analysis_error"] = f"读取失败 {exc}"[:200]
            return
        mime = item.get("mime") or mimetypes.guess_type(str(path))[0] or "image/jpeg"
        try:
            max_side = int(self.config.get("llm_max_image_side", 896) or 0)
        except (TypeError, ValueError):
            max_side = 896
        raw, mime = await asyncio.to_thread(self._prepare_image, raw, mime, max_side)
        data_url = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]

        meta: dict | None = None
        last_err = ""
        used = ""
        for ti, tgt in enumerate(targets):
            has_alt = ti + 1 < len(targets)
            meta, last_err = await self._try_target(item, tgt, messages, has_alt)
            if meta is not None:
                used = tgt["model"]
                break
            if has_alt:
                logger.warning(
                    f"[StickerKit] {tgt['model']} 打标失败（{item.get('name')}），"
                    f"改用候选模型 {targets[ti + 1]['model']}"
                )
        if meta is None:
            item["analysis_status"] = "failed"
            item["analysis_error"] = last_err or "打标失败"
            return
        self._last_model = used
        self._apply_meta(item, meta, used, force)

    async def _try_target(
        self, item: dict, tgt: dict, messages: list, has_alt: bool
    ) -> tuple[dict | None, str]:
        """拿一个模型打这一张。返回 (标注, 最后一条错误)，失败时标注为 None。

        has_alt=True 表示后面还有别的模型可换：这时候遇到限流就直接放弃本模型，
        把时间留给能用的那个；只有它是最后一根稻草才走「等冷却再重试」的老路。
        """
        payload = {
            "model": tgt["model"],
            "messages": messages,
            "max_tokens": 800,
            "temperature": 0.2,
        }
        # 打标是纯分类任务，不需要长思考。glm-5.x 这类「始终思考」的模型默认会烧
        # 400+ reasoning token（实测单张 20s），降到 low 后 3.6s，标注结果一模一样。
        effort = str(self.config.get("llm_reasoning_effort", "low") or "").strip().lower()
        if effort in ("low", "medium", "high", "max") and tgt["pk"] not in self._effort_bad:
            payload["reasoning_effort"] = effort
        headers = {"Authorization": f"Bearer {tgt['key']}", "Content-Type": "application/json"}

        async def _call_once() -> dict:
            async with self._http().post(
                f"{tgt['base']}/chat/completions", json=payload, headers=headers
            ) as resp:
                text = await resp.text()
                if resp.status in (429, 503):
                    try:
                        retry_after = float(resp.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0.0
                    raise _RateLimited(f"HTTP {resp.status}: {text[:150]}", retry_after)
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
                content = json.loads(text)["choices"][0]["message"].get("content")
            if isinstance(content, list):  # 少数服务商把 content 返回成分段数组
                content = "".join(
                    str(part.get("text") or "") for part in content if isinstance(part, dict)
                )
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

        meta = None
        last_err = ""
        for attempt in range(3):
            try:
                await self._llm_gate()
                meta = await _call_once()
                break
            except _RateLimited as exc:
                last_err = f"第{attempt + 1}次尝试: {exc}"[:200]
                cooldown = max(exc.retry_after, 10.0)
                # 按模型记熔断：后面排队的那几百张会先挑没冷却的那个用
                self._prov_down[tgt["pk"]] = max(
                    self._prov_down.get(tgt["pk"], 0.0), _now() + cooldown
                )
                if has_alt:
                    # 还有别的模型能顶上，不在这一棵树上等
                    logger.warning(
                        f"[StickerKit] {tgt['model']} 限流({item.get('name')}) {last_err}；"
                        f"该模型冷却 {cooldown:.0f}s，改用候选模型"
                    )
                    break
                # 它是唯一/最后一个可用的：设全队列冷却，让 _llm_gate 去等，
                # 不在这里 sleep（sleep 会一直占着 worker，把整个队列拖死）。
                self._llm_cooldown_until = max(self._llm_cooldown_until, _now() + cooldown)
                logger.warning(
                    f"[StickerKit] 上游限流/过载({item.get('name')}) {last_err}；全队列冷却 {cooldown:.0f}s"
                )
            except asyncio.CancelledError:
                raise
            except RuntimeError as exc:
                # 少数网关不认 reasoning_effort 会直接 400：自动摘掉该字段重试，
                # 并按模型记住，别让 100 张图每张都先失败一次。
                # 注意只有 400 才摘 —— 旧版无条件 pop，500 重试一次就悄悄退回
                # 「始终思考」，后面几张又变成 20 秒/张。
                if "HTTP 400" in str(exc) and "reasoning_effort" in payload:
                    payload.pop("reasoning_effort", None)
                    self._effort_bad.add(tgt["pk"])
                    last_err = f"第{attempt + 1}次尝试: {exc}"[:200]
                    logger.warning(
                        f"[StickerKit] {tgt['model']} 不接受 reasoning_effort（{exc}），"
                        f"该模型改用默认思考强度"
                    )
                    continue
                last_err = f"第{attempt + 1}次尝试: {exc}"[:200]
                logger.warning(f"[StickerKit] 打标失败({item.get('name')}) {last_err}")
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
            except Exception as exc:
                last_err = f"第{attempt + 1}次尝试: {exc}"[:200]
                logger.warning(f"[StickerKit] 打标失败({item.get('name')}) {last_err}")
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
        return meta, last_err

    def _apply_meta(self, item: dict, meta: dict, model: str, force: bool = False) -> None:
        """把模型输出写回条目。手改过的字段默认不覆盖（force=True 才盖）。"""
        human = set() if force else set(item.get("human") or [])
        emos_raw = [str(t).strip()[:12] for t in (meta.get("emotions") or []) if str(t).strip()]
        # 归一化到词表：词表外的先查别名（委屈巴巴→委屈、沉默→无语），
        # 同义情绪自然合并成一个（模型的「无语/沉默/嫌弃」其实是同一个判断）。
        emos: list[str] = []
        for e in emos_raw:
            canon = e if e in _EMOTION_SET else _ALIAS.get(e)
            if canon and canon in _EMOTION_SET and canon not in emos:
                emos.append(canon)
        if not emos and emos_raw:
            emos = emos_raw[:1]  # 全都不认识就留第一个，别把整条标注丢掉
        conf = self._norm_conf(meta.get("confidence"))
        if conf is not None and conf < 0.6:
            emos = emos[:1]  # 低置信：次情绪一律不要，只保留主情绪
        emos = emos[:2]
        tags: list[str] = []
        seen: set[str] = set()
        for t in meta.get("tags") or []:
            s = str(t).strip()[:12]
            if not s or s in seen or s in _EMOTION_SET or s in _ALIAS:
                continue  # 情绪词/别名混进 tags 会绕过主次权重，直接丢掉
            seen.add(s)
            tags.append(s)
        updates = {
            "name": str(meta.get("name") or item.get("name") or "未命名")[:16],
            "emotions": emos,
            "tags": tags[:4],
            "description": str(meta.get("description") or "")[:200],
            "visible_text": str(meta.get("visible_text") or "")[:200],
        }
        for field, val in updates.items():
            if field in human:
                continue
            if field in ("emotions", "tags") and not val:
                continue  # 模型没给就别清空已有标注
            item[field] = val
        item["analysis_meta"] = {
            "confidence": conf,
            "model": model,
            "at": _now(),
            "skipped": sorted(human & set(updates)),
        }
        item["analysis_status"] = "complete"
        item["analysis_error"] = ""
        if human:
            logger.info(f"[StickerKit] {item.get('name')} 打标完成，保留手改字段：{'/'.join(sorted(human))}")

    # ---------- 偷表情包 ----------
    #
    # 难点在「怎么看见表情」：AstrBot 的 aiocqhttp 适配器在解析消息段时对 mface
    # 直接 `continue` 丢掉了（见 aiocqhttp_platform_adapter.py 的 `elif t == "mface"`），
    # 所以 event.message_obj.message 里根本不会有它。
    # 只能绕到底层：abm.raw_message 存的就是原始 OneBot 事件，里面的 message 段数组
    # 还带着 mface（含直链 url），从这里捞。

    _STEAL_TTL = 300.0   # 候选存活时间（秒）
    _STEAL_MAX = 6       # 单个会话最多留几张

    def _steal_on(self) -> bool:
        return bool(self.config.get("steal_enabled", False))

    def _steal_auto(self) -> bool:
        """偷来的图直接进库（不需要人工审核）。"""
        return bool(self.config.get("steal_auto_approve", False))

    @staticmethod
    def _raw_segments(event) -> list:
        """取原始 OneBot 消息段数组；拿不到就返回空表（非 aiocqhttp 平台就是空）。"""
        try:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        except Exception:
            return []
        if raw is None:
            return []
        segs = None
        try:
            if isinstance(raw, dict):
                segs = raw.get("message")
            else:
                segs = getattr(raw, "message", None)
                if segs is None and hasattr(raw, "get"):
                    segs = raw.get("message")
        except Exception:
            segs = None
        return segs if isinstance(segs, list) else []

    def _grab_media(self, event) -> list[dict]:
        """从原始消息里挑出可偷的图，带上直链。没有直链的跳过。

        QQ 的两种形态都收：
        - mface：商城表情/魔法表情，带 emoji_id，是「真正的」QQ 表情
        - image：收藏表情、图片表情、转发图，QQ 客户端就是把它们当图片发出去的，
          段类型是 image 而不是 mface（只认 mface 会漏掉绝大多数「表情包」）
        """
        out: list[dict] = []
        for seg in self._raw_segments(event):
            if not isinstance(seg, dict):
                continue
            kind = seg.get("type")
            if kind not in ("mface", "image"):
                continue
            data = seg.get("data")
            if not isinstance(data, dict):
                continue
            url = str(data.get("url") or data.get("file") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                continue  # 有些实现只给个文件名，下不了
            if kind == "mface":
                summary = str(data.get("summary") or "")[:40]
            else:
                summary = _readable_name(data.get("summary")) or _readable_name(data.get("file"))
            out.append({
                "url": url,
                "kind": kind,
                "summary": summary,
                "emoji_id": str(data.get("emoji_id") or "")[:40] if kind == "mface" else "",
            })
        return out

    def _grab_mface(self, event) -> list[dict]:
        """只要 QQ 原生表情（mface）。保留给只关心商城表情的地方用。"""
        return [c for c in self._grab_media(event) if c.get("kind") == "mface"]

    def _remember_media(self, event, umo: str) -> list[dict]:
        """把这一轮的表情/图片记进会话候选池，返回这次新增的。

        注意：**谁发的都记**。真正要偷的是「别人发的」表情，如果这里卡管理员，
        群友发一百张也进不了池子，管理员说「偷这张」时永远是空的。
        能不能调用工具是另一回事（`_admin_check` 在工具里管）。
        """
        now = _now()
        pool = [c for c in self._steal_pool.get(umo, []) if now - float(c.get("at") or 0) < self._STEAL_TTL]
        fresh: list[dict] = []
        for item in self._grab_media(event):
            if any(c.get("url") == item["url"] for c in pool):
                continue
            rec = dict(item, at=now)
            pool.append(rec)
            fresh.append(rec)
        self._steal_pool[umo] = pool[-self._STEAL_MAX:]
        # 顺手清理过期会话，别让这个 dict 无上限长下去
        if len(self._steal_pool) > 40:
            for k in [
                k for k, v in self._steal_pool.items()
                if not v or now - float(v[-1].get("at") or 0) > self._STEAL_TTL
            ]:
                self._steal_pool.pop(k, None)
        return fresh

    def _remember_mface(self, event, umo: str) -> list[dict]:
        """兼容旧名，行为同 `_remember_media`。"""
        return self._remember_media(event, umo)

    def _steal_candidates(self, umo: str) -> list[dict]:
        now = _now()
        pool = [c for c in self._steal_pool.get(umo, []) if now - float(c.get("at") or 0) < self._STEAL_TTL]
        if pool:
            self._steal_pool[umo] = pool
        else:
            self._steal_pool.pop(umo, None)
        return pool

    def _steal_hint(self, event) -> str:
        """给 AI 的提示。没开功能 / 没权限时返回空串（但候选照记）。

        分三种：
        - 这条带 QQ 表情（mface）→ 可以让 AI 顺口问一句要不要收
        - 这条带普通图片 → 只记候选，不主动问（别对别人的自拍嘘寒问暖），
          用户明说要偷时工具自然能拿到
        - 只是手上有存货 → 一条短提醒，免得 AI 在纯文字聊天里反复问
        """
        if not self._steal_on():
            return ""
        umo = getattr(event, "unified_msg_origin", "") or ""
        fresh = self._remember_media(event, umo)
        if self.config.get("steal_admin_only", False) and not event.is_admin():
            return ""  # 群友发的照样记下来，但不给提示（他们也不能偷）
        if self._steal_quota(event):
            return ""  # 冷却中 / 今日额度满：别让 AI 白问一句再被工具打回，白烧一轮 token
        mf = [c for c in fresh if c.get("kind") == "mface"]
        if mf:
            return (
                "【偷表情包】对方这条消息里带 QQ 表情。可以顺手在回复末尾问一句要不要收进表情包库"
                "（用户表示想收/想偷时，调用 sticker_steal 工具收录；用户没兴趣就别问，也别调用工具）。"
            )
        if fresh:
            return (
                "【偷表情包】对方这条消息里带图片，已经记成可偷候选。"
                "用户明确说要偷/要收的时候调用 sticker_steal 收录；用户没提就别主动问，"
                "更不要自己决定替他收下来。"
            )
        if self._steal_candidates(umo):
            # 上一轮问过、用户这轮才说「要」——只提醒工具还能用，不再让 AI 主动发问
            return "【偷表情包】手上还有刚收到的表情包，用户说要/想收的话调用 sticker_steal 收录。"
        return ""

    @staticmethod
    def _img_complete(raw: bytes, mime: str, cl: int = 0) -> bool:
        """粗校验图是不是下完整了。

        QQ 图床偶尔半路掐连接。渐进式 JPEG 只收到上半截也能解出个样子，
        存进库就是「只有上面一丢丢」的破图，所以存盘前先看尾部标记。
        检查故意放宽（往最后十几个字节里找），免得带 padding 的图被误杀。
        """
        n = len(raw)
        if n < 16:
            return False
        if cl and n != cl:
            return False
        if mime == "image/jpeg":
            return b"\xff\xd9" in raw[-16:]
        if mime == "image/png":
            return b"IEND" in raw[-16:]
        if mime == "image/gif":
            return b"\x3b" in raw[-8:]
        if mime == "image/webp":
            riff = int.from_bytes(raw[4:8], "little") + 8
            return riff == 0 or abs(riff - n) <= 8
        return True

    async def _download_image(self, url: str) -> tuple[bytes, str]:
        """下表情图。返回 (原始字节, mime)；超过体积上限直接报错。

        坑：aiohttp 的 `StreamReader.read(n)` 语义是「最多 n 字节」，数据没全到齐时
        它会把手上那点先返回。以前写成 `read(max_bytes + 1)`，结果每次只拿到第一个
        chunk（正好 16384 字节），存进去就是半张图。必须 iter_chunked 读到 EOF。
        """
        try:
            mb = int(float(self.config.get("steal_max_mb", 8) or 8))
        except (TypeError, ValueError):
            mb = 8
        max_bytes = max(1, min(mb, 32)) * 1024 * 1024
        timeout = aiohttp.ClientTimeout(total=30, sock_read=20)
        last = ""
        for attempt in range(2):
            buf = bytearray()
            cl = 0
            try:
                async with self._http().get(url, timeout=timeout) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"下载失败 HTTP {resp.status}")
                    cl = int(resp.headers.get("Content-Length") or 0)
                    if cl and cl > max_bytes:
                        raise RuntimeError(f"图片太大（{cl / 1024 / 1024:.1f}MB）")
                    async for chunk in resp.content.iter_chunked(65536):
                        buf += chunk
                        if len(buf) > max_bytes:
                            raise RuntimeError(f"图片超过 {max_bytes // 1024 // 1024}MB")
            except RuntimeError as exc:
                msg = str(exc)
                if msg.startswith(("下载失败", "图片太大", "图片超过")):
                    raise  # 这些是明确结论，重试没意义
                last = f"{type(exc).__name__}: {msg}"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                raw = bytes(buf)
                if not raw:
                    raise RuntimeError("下载到的是空文件")
                mime = _sniff_image(raw)
                if not mime:
                    raise RuntimeError("下载到的不是图片（可能是过期链接）")
                if self._img_complete(raw, mime, cl):
                    return raw, mime
                last = f"只收到 {len(raw)}/{cl or '?'} 字节"
            if attempt == 0:
                await asyncio.sleep(0.4)  # 连接被掐的概率不小，重下一次
        raise RuntimeError(f"图片没下完整（{last}），链接可能已经失效")

    async def _steal_one(self, event, index: int = 0) -> str:
        """把候选里的第 index 张（0 = 最新）收进库。返回给 AI 看的结果文字。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        pool = self._steal_candidates(umo)
        if not pool:
            return (
                "没有可偷的表情包：对方最近 5 分钟内没发过表情包/图片，或者已经处理过了。"
                "这次不要收录，如实告诉用户没找到（不要猜是什么格式问题，就是没存货）。"
            )
        i = max(0, min(int(index or 0), len(pool) - 1))
        cand = pool[-1 - i] if i < len(pool) else pool[-1]
        try:
            raw, mime = await self._download_image(cand["url"])
        except Exception as exc:
            return f"这张表情包没下下来（{type(exc).__name__}: {exc}），链接可能已经过期。如实告诉用户失败即可，不要重试。"

        sha = hashlib.sha256(raw).hexdigest()
        dup = next((it for it in self._items if it.get("sha256") == sha), None)
        if dup is not None:
            pool.remove(cand)
            if not pool:
                self._steal_pool.pop(umo, None)
            return (
                f"这张已经在库里面了（「{dup.get('name')}」，来源：{SOURCE_LABEL.get(str(dup.get('source') or ''), '未知')}），"
                "不用重复收录。直接告诉用户已经有了。"
            )

        ext = _MIME_EXT.get(mime, ".png")
        stored = f"{uuid.uuid4().hex}{ext}"
        try:
            await asyncio.to_thread((self._img_dir / stored).write_bytes, raw)
        except Exception as exc:
            return f"存文件失败：{exc}。如实告诉用户没存进去。"

        auto = self._steal_auto()
        kind = str(cand.get("kind") or "mface")
        name = (cand.get("summary") or "").strip()[:40]
        if not name or name.lower().startswith(("http", "image")):
            name = "偷来的表情" if kind == "mface" else "偷来的图片"
        item = {
            "id": uuid.uuid4().hex,
            "stored_name": stored,
            "sha256": sha,
            "name": name,
            "tags": [],
            "emotions": [],
            "intents": [],
            "description": "",
            "visible_text": "",
            "enabled": True,
            "size": len(raw),
            "mime": mime,
            "analysis_status": "none",
            "analysis_error": "",
            "source": "steal",
            "review": "approved" if auto else "pending",
            "stolen_from": {
                "url": cand.get("url") or "",
                "emoji_id": cand.get("emoji_id") or "",
                "kind": kind,
                "session": umo[:96],
                "at": _now(),
            },
            "created_at": _now(),
        }
        self._items.append(item)
        pool.remove(cand)
        if not pool:
            self._steal_pool.pop(umo, None)

        # 记下这次是「哪条消息」偷的：同一条消息里再偷几张不吃冷却
        mid = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        if mid:
            if len(self._steal_last_msg) > 200:
                self._steal_last_msg.clear()  # 纯防抖用，清掉不影响正确性
            self._steal_last_msg[umo] = mid

        queued = False
        # 待审核的先不打标：万一用户不要，这次 LLM 钱就白花了。等审核通过再补。
        if auto and bool(self.config.get("auto_tag_on_upload", True)):
            queued = self._queue_analysis(item)
        self._save_index(immediate=True)

        if auto:
            tail = "已经直接进库了" + ("，正在排队打标。" if queued else "。")
        else:
            tail = "已放进「待审核」，管理员在面板里点允许后才会参与检索。"
        what = "一张表情包" if kind == "mface" else "一张表情图"
        return f"收好了：{what}，{tail}直接回用户一句就好，不要说技术细节。"

    # ---------- 打标队列（worker 池） ----------

    def _ensure_workers(self) -> None:
        want = self._llm_conc()
        if len(self._workers) == want and all(not w.done() for w in self._workers):
            return
        if self._active_ids:
            return  # 有请求在飞，别在半路重建池子
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # 没有事件循环（插件构造阶段），等真正入队时再起
        self._restart_workers()

    def _restart_workers(self) -> None:
        for w in self._workers:
            if not w.done():
                w.cancel()
        self._workers = []
        if self._queue.empty():
            self._queued_ids.clear()
        self._worker_count = self._llm_conc()
        for idx in range(self._worker_count):
            self._workers.append(asyncio.ensure_future(self._worker_loop(idx)))
        logger.info(f"[StickerKit] 打标 worker 池已启动：{self._worker_count} 个并发")

    async def _worker_loop(self, idx: int) -> None:
        while True:
            # 暂停时在这里等，队列里的任务原样留着；恢复后接着跑。
            await self._resume_evt.wait()
            item, force = await self._queue.get()
            iid = item.get("id")
            # 按下暂停的那一瞬间刚好 get 到手的这张：放回队尾，等恢复再跑。
            if self._queue_paused:
                self._queue.put_nowait((item, force))
                self._queue.task_done()
                await self._resume_evt.wait()
                continue
            try:
                if not any(it is item for it in self._items):
                    continue  # 排队期间被删掉了
                self._active_ids.add(iid)
                self._active_since[iid] = _now()
                self._queued_at.pop(iid, None)
                await self._analyze_item(item, force)
                self._queue_done += 1
                self._done_times.append(_now())
            except asyncio.CancelledError:
                self._queued_ids.discard(iid)
                raise
            except Exception as exc:
                self._queue_failed += 1
                logger.warning(
                    f"[StickerKit] 打标异常({item.get('name')}): {type(exc).__name__} {exc}"
                )
                item["analysis_status"] = "failed"
                item["analysis_error"] = f"{type(exc).__name__}: {exc}"[:200]
            finally:
                self._queued_ids.discard(iid)
                self._active_ids.discard(iid)
                self._active_since.pop(iid, None)
                self._queue.task_done()
                if self._queue.empty() and not self._active_ids:
                    self._save_index(immediate=True)

    def _queue_analysis(self, item: dict, force: bool = False) -> bool:
        if not self._llm_ready():
            return False
        iid = item.get("id")
        if not iid or iid in self._queued_ids:
            return False
        self._queued_ids.add(iid)
        self._queued_at[iid] = _now()
        item["analysis_status"] = "pending"
        item["analysis_error"] = ""
        self._queue.put_nowait((item, force))
        self._ensure_workers()
        return True

    def _queue_rate(self) -> float:
        """最近 30 张的平均速度（张/秒）。只取最近完成的时间戳，早期慢的不拖累估算。"""
        if len(self._done_times) < 2:
            return 0.0
        span = self._done_times[-1] - self._done_times[0]
        if span <= 0:
            return 0.0
        return (len(self._done_times) - 1) / span

    def _queue_stats(self) -> dict:
        pending = sum(1 for it in self._items if it.get("analysis_status") == "pending")
        failed = sum(1 for it in self._items if it.get("analysis_status") == "failed")
        left = self._queue.qsize() + len(self._active_ids)
        rate = self._queue_rate()
        return {
            "queued": self._queue.qsize(),
            "active": len(self._active_ids),
            "pending": pending,
            "failed": failed,
            "done": self._queue_done,
            "error": self._queue_failed,
            "workers": len([w for w in self._workers if not w.done()]),
            "cooldown": round(max(0.0, self._llm_cooldown_until - _now()), 1),
            "paused": self._queue_paused,
            "left": left,
            "rate": round(rate, 2),
            "eta": round(left / rate, 1) if rate > 0 and left else 0,
            "total": len(self._items),
        }

    # ---------- 队列控制 ----------

    def _set_paused(self, paused: bool) -> None:
        self._queue_paused = bool(paused)
        if self._queue_paused:
            self._resume_evt.clear()
        else:
            self._resume_evt.set()
        logger.info(f"[StickerKit] 打标队列{'已暂停' if paused else '已继续'}")

    def _drop_queued(self) -> int:
        """清空待处理队列（不动正在飞的那几张），把它们退回「未打标」。"""
        dropped = 0
        while True:
            try:
                item, _force = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            iid = item.get("id")
            self._queued_ids.discard(iid)
            self._queued_at.pop(iid, None)
            if item.get("analysis_status") == "pending":
                item["analysis_status"] = "none"
                item["analysis_error"] = ""
            dropped += 1
            self._queue.task_done()
        if dropped:
            self._save_index(immediate=True)
        return dropped

    # ---------- 面板 Web API ----------

    def _register_panel(self) -> None:
        register = self.context.register_web_api
        register(f"{PANEL_PREFIX}", self._panel_page, ["GET"], "StickerKit 面板")
        register(f"{API_PREFIX}/list", self._api_list, ["GET"], "表情包列表")
        register(f"{API_PREFIX}/upload", self._api_upload, ["POST"], "批量上传")
        register(f"{API_PREFIX}/import_dir", self._api_import_dir, ["POST"], "文件夹导入")
        register(f"{API_PREFIX}/update", self._api_update, ["POST"], "修改标注")
        register(f"{API_PREFIX}/delete", self._api_delete, ["POST"], "删除")
        register(f"{API_PREFIX}/delete_batch", self._api_delete_batch, ["POST"], "批量删除")
        register(f"{API_PREFIX}/review", self._api_review, ["GET"], "待审核列表")
        register(f"{API_PREFIX}/review/apply", self._api_review_apply, ["POST"], "审核偷来的图")
        register(f"{API_PREFIX}/image", self._api_image, ["GET"], "图片预览")
        register(f"{API_PREFIX}/analyze", self._api_analyze, ["POST"], "重新打标")
        register(f"{API_PREFIX}/queue", self._api_queue, ["GET"], "打标队列")
        register(f"{API_PREFIX}/queue/pause", self._api_queue_pause, ["POST"], "队列暂停/继续")
        register(f"{API_PREFIX}/queue/clear", self._api_queue_clear, ["POST"], "清空待打标")
        register(f"{API_PREFIX}/queue/retry", self._api_queue_retry, ["POST"], "批量重新排队")
        register(f"{API_PREFIX}/config", self._api_config, ["GET"], "配置读取")
        register(f"{API_PREFIX}/config/set", self._api_config_set, ["POST"], "配置保存")
        register(f"{API_PREFIX}/brief", self._api_brief, ["GET"], "库存概览设置")
        register(f"{API_PREFIX}/brief/set", self._api_brief_set, ["POST"], "库存概览保存")

    async def _panel_page(self):
        page = Path(__file__).parent / "pages" / "表情包小屋" / "index.html"
        return await send_file(str(page))

    def _ok(self, data=None) -> dict:
        return {"status": "ok", "data": data or {}}

    def _err(self, msg: str) -> dict:
        return {"status": "error", "message": msg}

    # ---------- WebUI 配置页 ----------

    _CFG_GROUPS = (
        (
            "配图频率",
            (
                "sticker_frequency",
                "send_probability",
                "send_cooldown_seconds",
                "named_request_bypass",
                "named_request_keywords",
            ),
        ),
        (
            "协议与文案",
            (
                "protocol_mode",
                "brief_mode",
                "brief_budget",
                "brief_groups",
                "protocol_prompt_extra",
                "custom_protocol_prompt",
                "inject_to_user_prompt",
                "delivery_mode",
                "image_format",
            ),
        ),
        ("AI 工具与发送", ("admin_only", "enable_send", "enable_search", "top_k", "search_detail", "list_page_size", "separate_send_gap_ms", "write_lock_seconds")),
        ("检索与导入", ("fuzzy_search", "min_score", "min_margin", "blocked_tags", "auto_tag_on_upload", "max_image_mb")),
        ("LLM 视觉打标", ("llm_source", "llm_provider_id", "llm_provider_pool", "llm_api_base", "llm_api_key", "llm_model", "llm_concurrency", "llm_min_interval", "llm_max_image_side", "llm_reasoning_effort")),
        ("偷表情包", ("steal_enabled", "steal_admin_only", "steal_auto_approve",
                     "steal_cooldown", "steal_daily_limit", "steal_max_mb")),
    )

    @property
    def _cfg_path(self) -> Path:
        return DATA_ROOT / "config" / f"{PLUGIN_NAME}_config.json"

    def _cfg_schema(self) -> dict:
        try:
            return json.loads((Path(__file__).parent / "_conf_schema.json").read_text(encoding="utf-8-sig"))
        except Exception as exc:
            logger.warning(f"[StickerKit] 读取配置 schema 失败: {exc}")
            return {}

    @staticmethod
    def _short_label(desc: str) -> str:
        head = re.split(r"[：:；（(]", str(desc or ""), 1)[0].strip()
        return head[:30] or "未命名配置"

    def _cfg_items(self) -> list[dict]:
        schema = self._cfg_schema()
        try:
            cur = dict(self.config)
        except Exception:
            cur = {}
        provs = self._provider_candidates()

        def mk(k: str, meta: dict, group: str) -> dict:
            options = meta.get("options") or []
            hint = str(meta.get("description") or "")
            if k == "llm_provider_id":
                options = [{"value": "", "label": "自动（用 AstrBot 当前的默认对话模型）"}]
                options += [
                    {"value": c["id"], "label": f'{c["model"]} — {c["id"]}'} for c in provs
                ]
                if provs:
                    hint = (
                        "直接复用 AstrBot 里已经配好的模型（只列 OpenAI 兼容的类型）。"
                        "留空 = 跟随 AstrBot 当前的默认对话模型，手填的 Key 只在提供商读不到时兜底。"
                    )
                else:
                    hint = (
                        "【没读到可用的 AstrBot 提供商】先去 AstrBot 的「服务提供商」配一个能看图的"
                        " OpenAI 兼容模型；不想配就继续用下面三项手填。"
                    )
            elif k == "llm_provider_pool":
                options = [
                    {"value": c["id"], "label": f'{c["model"]} — {c["id"]}'} for c in provs
                ]
                if provs:
                    hint = (
                        "勾几个备用模型：主模型被限流/报错时自动换下一个，图片只读一次，"
                        "换模型不多花读盘和缩放的时间。不勾也能用，勾了更抗打。"
                        "（按上面的先后顺序试）"
                    )
                    try:
                        has = len(self._llm_targets())
                    except Exception:
                        has = 0
                    if has:
                        hint += f"　·　当前生效 {has} 个：{self._llm_source_desc()}"
                else:
                    hint = (
                        "【没读到可用的 AstrBot 提供商】候选模型只能从 AstrBot 已配置的"
                        " OpenAI 兼容提供商里挑，先去「服务提供商」加一个。"
                    )
            elif k == "steal_enabled":
                pend = sum(1 for it in self._items if _review_state(it) == "pending")
                hint = (
                    "打开后，别人发 QQ 表情时 AI 会顺口问一句要不要收；"
                    "当图片发出来的表情图不主动问，你说要偷才收。关掉=完全不理会别人的表情。"
                    "谁能偷由下面的「只允许管理员偷」决定。"
                )
                if pend:
                    hint += f"　·　另有 {pend} 张待审核"
            elif k == "steal_admin_only":
                only = bool(self.config.get("steal_admin_only", False))
                hint = (
                    "关着=群里谁都能喊「偷这张」，收下的图照样先进「待审核」，"
                    "不会直接影响库里的图；开着=只有管理员能触发。"
                    "现在：" + ("只有管理员能偷" if only else "谁都能偷（先进待审核）")
                )
            elif k == "steal_auto_approve":
                hint = (
                    "开=偷来的直接进库并排队打标；关=先进「待审核」，面板里点允许才参与检索。"
                    "待审核阶段不打标，免得你点了拒绝还白花一次 LLM 调用。"
                )
                if not self.config.get("steal_admin_only", False):
                    hint += "　·　注意：「只允许管理员偷」关着，再开这个等于群里谁都能往库里塞图。"
            elif k == "steal_cooldown":
                try:
                    cd = float(self.config.get("steal_cooldown", 30) or 0)
                except (TypeError, ValueError):
                    cd = 30.0
                hint = (
                    "同一个会话两次偷之间至少隔多少秒，防止被一张接一张地刷。0 = 不限制。"
                    "同一条消息里连着偷几张（用户一口气发三张说都要）不算刷，不会被拦。"
                    + ("　·　当前：不限制" if cd <= 0 else f"　·　当前：{cd:.0f} 秒")
                )
            elif k == "steal_daily_limit":
                try:
                    lim = int(float(self.config.get("steal_daily_limit", 20) or 0))
                except (TypeError, ValueError):
                    lim = 20
                used = self._steal_used_today()
                hint = (
                    "按入库时间统计今天偷了几张，满额后当天不再收录，第二天自动恢复。0 = 不限制。"
                    "重启不会把额度清零；在面板里删掉几张，额度也跟着还回来。"
                )
                if lim <= 0:
                    hint += f"　·　当前：不限制（今天已偷 {used} 张）"
                else:
                    hint += f"　·　当前：{used}/{lim} 张" + ("（今天已满额）" if used >= lim else "")
            elif k == "steal_max_mb":
                hint = "单位 MB。超过这个大小的图直接跳过不下载，防止别人发个几十兆的视频表情把盘塞满。"
            elif k == "llm_source":
                hint = (
                    "自动 = 下面三项填全了就用它们，否则跟随 AstrBot 的提供商；"
                    "跟随 AstrBot = 只用上面选的提供商；手动填写 = 只用下面三项。"
                )
                hint += f"　·　当前实际使用：{self._llm_source_desc()}"
            return {
                "key": k,
                "group": group,
                "type": meta.get("type", "string"),
                "value": cur.get(k, meta.get("default")),
                "options": options,
                "hint": hint,
                "label": self._short_label(meta.get("description")),
                "secret": k == "llm_api_key",
                "multi": k == "llm_provider_pool",
            }

        items, placed = [], set()
        for group, keys in self._CFG_GROUPS:
            for k in keys:
                meta = schema.get(k)
                if not meta:
                    continue
                placed.add(k)
                items.append(mk(k, meta, group))
        for k, meta in schema.items():
            if k in placed:
                continue
            placed.add(k)
            items.append(mk(k, meta, "其他"))
        return items

    def _write_cfg_file(self) -> str:
        try:
            data = dict(self.config)
        except Exception:
            data = {}
        try:
            self._cfg_path.parent.mkdir(parents=True, exist_ok=True)
            self._cfg_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8-sig"
            )
            return "file"
        except Exception as exc:
            logger.warning(f"[StickerKit] 配置直接写文件失败: {exc}")
            return "none"

    def _apply_runtime(self) -> None:
        """配置变更后重建运行时限流对象，立即生效。"""
        try:
            interval = max(0.0, float(self.config.get("llm_min_interval", 1.0) or 0))
        except (TypeError, ValueError):
            interval = 1.0
        self._llm_min_interval = interval
        conc = self._llm_conc()
        # 并发数改了要重建 worker 池；池子里有活的时候等它跑完再换，避免打断正在飞的请求
        if len(self._workers) != conc or any(w.done() for w in self._workers):
            if self._active_ids:
                logger.info("[StickerKit] 并发数已改，等当前批次跑完再重建 worker 池")
            else:
                self._restart_workers()
        logger.info(f"[StickerKit] 配置已热更新：并发={conc} 间隔={interval}s 模糊搜索={self._fuzzy_enabled()}")

    async def _api_config(self):
        return self._ok({
            "items": self._cfg_items(),
            "path": str(self._cfg_path),
        })

    async def _api_config_set(self):
        data = await quart_request.get_json(silent=True) or {}
        patch = data.get("patch") if isinstance(data.get("patch"), dict) else data
        if not patch:
            return self._err("没有要保存的改动")
        schema = self._cfg_schema()
        applied = {}
        for k, v in patch.items():
            meta = schema.get(k)
            if not meta:
                continue
            t = meta.get("type", "string")
            try:
                if t == "bool":
                    if isinstance(v, str):
                        v = v.strip().lower() in ("1", "true", "on", "yes", "开")
                    v = bool(v)
                elif t == "int":
                    v = int(float(v))
                elif t == "float":
                    v = float(v)
                else:
                    if k == "llm_provider_pool":
                        # 多选：前端给的是 id 数组或逗号串，只留 AstrBot 里还存在的
                        raw = (
                            v if isinstance(v, (list, tuple))
                            else re.split(r"[,，;；\s]+", str(v))
                        )
                        ids = [c["id"] for c in self._provider_candidates() if c.get("id")]
                        if not ids:
                            continue  # 这会儿读不到提供商，别把已选好的清空
                        keep: list[str] = []
                        for x in raw:
                            x = str(x).strip()
                            if x and x in ids and x not in keep:
                                keep.append(x)
                        v = ",".join(keep)
                    else:
                        v = str(v).strip()
                        opts = meta.get("options")
                        if opts:
                            allowed = {o.get("value") if isinstance(o, dict) else o for o in opts}
                            if v not in allowed:
                                continue
                        if k == "llm_provider_id" and v:
                            ids = {c["id"] for c in self._provider_candidates()}
                            if ids and v not in ids:
                                continue  # 选中的提供商已经不在了（被删/改名），别静默存进去
            except (TypeError, ValueError):
                continue
            if k == "llm_api_key" and not v:
                continue  # 密钥留空 = 保持不变，防止误清空
            applied[k] = v
        if not applied:
            return self._err("没有有效的改动（未知键或空密钥）")
        for k, v in applied.items():
            try:
                self.config[k] = v
            except Exception:
                logger.warning(f"[StickerKit] 写入内存配置 {k} 失败")
        mode = self._persist_config()
        return self._ok({
            "saved": True,
            "mode": mode,
            "items": self._cfg_items(),
            "path": str(self._cfg_path),
        })

    def _persist_config(self) -> str:
        """把内存里的 config 落盘（优先用宿主的 save_config），并热更新运行参数。"""
        mode = "memory"
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            try:
                saver()
                mode = "saved"
            except Exception:
                logger.warning("[StickerKit] save_config 失败，退回直接写文件", exc_info=True)
                mode = self._write_cfg_file()
        else:
            mode = self._write_cfg_file()
        self._apply_runtime()
        return mode

    # ---------- 面板「注入分类」页：库存概览的设置与预览 ----------

    async def _api_brief(self):
        view = self._brief_render()
        picked = set(view.get("picked") or [])
        cats = [
            {"word": w, "count": c, "weight": wt, "selected": w in picked}
            for w, c, wt in self._emotion_counts()
        ]
        return self._ok({
            "mode": self._brief_mode(),
            "budget": self._brief_budget(),
            "groups": self._brief_groups(),
            "categories": cats,
            "preview": view.get("text") or "",
            "used": int(view.get("used") or 0),
            "all_used": int(view.get("all_used") or 0),
            "over": self._brief_mode() == "manual"
            and int(view.get("all_used") or 0) > int(view.get("budget") or 1),
            "truncated": bool(view.get("truncated")),
            "total": int(view.get("total") or 0),
            "protocol_on": bool(self.config.get("protocol_mode", True)),
            "frequency": self._frequency_level(),
        })

    async def _api_brief_set(self):
        data = await quart_request.get_json(silent=True) or {}
        raw_mode = str(data.get("mode") or "自动").strip()
        mode = {"自动": "auto", "手动": "manual", "关闭": "off"}.get(raw_mode)
        if mode is None:
            return self._err("未知的注入模式")
        try:
            budget = int(float(data.get("budget", self._brief_budget())))
        except (TypeError, ValueError):
            return self._err("预算必须是数字")
        budget = max(20, min(600, budget))
        if "groups" in data:
            raw_groups = data.get("groups")
            if isinstance(raw_groups, str):
                raw_groups = re.split(r"[,，、\s]+", raw_groups)
            groups = [str(w).strip() for w in (raw_groups or []) if str(w).strip()]
        else:
            groups = self._brief_groups()
        view = self._brief_render(mode, budget, groups)
        # 自动模式本来就该"挑到预算用完"，超出的部分自动截断不算错；
        # 只有手动勾选超出预算才拦下来，让用户自己减或加预算。
        over = mode == "manual" and int(view.get("all_used") or 0) > budget
        payload = {
            "mode": mode,
            "preview": view.get("text") or "",
            "used": int(view.get("used") or 0),
            "all_used": int(view.get("all_used") or 0),
            "budget": budget,
            "over": over,
            "truncated": bool(view.get("truncated")),
            "picked": view.get("picked") or [],
        }
        if data.get("dry"):
            # 面板拖动/勾选时的实时预览，不落盘
            return self._ok({**payload, "dry": True})
        if over:
            return self._err(
                "当前库存已达到预算上限！请去设置里自行调整"
                f"（勾选的分类需要约 {view['all_used']} token，当前预算 {budget}）"
            )
        self.config["brief_mode"] = raw_mode
        self.config["brief_budget"] = budget
        if mode == "manual":
            self.config["brief_groups"] = "，".join(groups)
        saved = self._persist_config()
        return self._ok({**payload, "saved": True, "save_mode": saved})


    def _item_view(self, it: dict) -> dict:
        """给面板的条目视图。别把 stolen_from 里的原图直链带出去，用不上还占带宽。"""
        src = str(it.get("source") or "")
        return {
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
            "confidence": (it.get("analysis_meta") or {}).get("confidence"),
            "human": it.get("human") or [],
            "source": src,
            "source_label": SOURCE_LABEL.get(src, "未知来源"),
            "review": _review_state(it),
            "stolen_at": (it.get("stolen_from") or {}).get("at") if src == "steal" else None,
            "usage_count": it.get("usage_count", 0),
            "created_at": it.get("created_at"),
        }

    async def _api_list(self):
        items = [
            self._item_view(it)
            for it in sorted(self._items, key=lambda x: -float(x.get("created_at") or 0))
        ]
        llm_ready = self._llm_ready()
        return {
            "status": "ok",
            "data": {
                "items": items,
                "llm_ready": llm_ready,
                "data_dir": str(self._data_dir),
                "queue": self._queue_stats(),
                "llm": self._llm_source_desc() if llm_ready else "",
                "version": "1.10.3",
            },
        }

    async def _api_upload(self):
        all_files = await quart_request.files
        files = all_files.getlist("files") or all_files.getlist("file")
        if not files:
            return self._err("没有收到文件")
        form = await quart_request.form  # 提到循环外，旧版每张图都取一次
        form_desc = str(form.get("description") or "")[:200]
        max_bytes = int(self.config.get("max_image_mb", 10) or 10) * 1024 * 1024
        known_shas = {it.get("sha256") for it in self._items}
        auto_tag = bool(self.config.get("auto_tag_on_upload", True))
        saved, dup, rejected = [], [], []
        queued = 0
        for f in files:
            ext = Path(f.filename or "").suffix.lower()
            if ext not in ALLOWED_EXT:
                rejected.append(f"{f.filename}: 不支持的格式")
                continue
            # 大文件 io 放到线程里，避免 100 张上传时把事件循环堵死（面板会像卡住一样）
            raw = await asyncio.to_thread(f.read)
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
            await asyncio.to_thread((self._img_dir / stored).write_bytes, raw)
            item = {
                "id": uuid.uuid4().hex,
                "stored_name": stored,
                "sha256": sha,
                "name": Path(f.filename or stored).stem[:40],
                "tags": [],
                "emotions": [],
                "intents": [],
                "description": form_desc,
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
            # 旧版这里「填了描述就跳过打标」，导致那张图 tags/emotions 全空、几乎检索不到。
            # 现在照常排队，手填的字段会被 human 标记保护，打标只补空缺。
            if auto_tag and self._queue_analysis(item):
                queued += 1
        if saved:
            self._save_index(immediate=True)
        return self._ok(
            {
                "saved": len(saved),
                "duplicated": dup,
                "rejected": rejected,
                "ids": [it["id"] for it in saved],
                "queued": queued,
                "llm_ready": all(self._llm_conf()),
            }
        )

    async def _api_import_dir(self):
        """按本地路径导入一个文件夹里的图片（桌面端拖拽被壳拦截时的批量兜底）。"""
        payload = await quart_request.get_json(silent=True) or {}
        raw_path = str(payload.get("path") or "").strip().strip('"')
        if not raw_path:
            return self._err("请填写文件夹路径")
        root = Path(raw_path)
        if not root.exists() or not root.is_dir():
            return self._err(f"目录不存在：{raw_path[:120]}")
        try:
            candidates = sorted(
                (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in ALLOWED_EXT),
                key=lambda p: p.stat().st_mtime,
            )
        except Exception as exc:
            return self._err(f"扫描目录失败：{exc}"[:200])
        limit = 500
        truncated = len(candidates) > limit
        candidates = candidates[:limit]
        max_bytes = int(self.config.get("max_image_mb", 10) or 10) * 1024 * 1024
        known_shas = {it.get("sha256") for it in self._items}
        auto_tag = bool(self.config.get("auto_tag_on_upload", True))
        saved, dup, rejected = [], [], []
        queued = 0
        for src in candidates:
            try:
                raw = await asyncio.to_thread(src.read_bytes)
            except Exception as exc:
                rejected.append(f"{src.name}: 读取失败 {exc}"[:80])
                continue
            if len(raw) > max_bytes:
                rejected.append(f"{src.name}: 超过 {max_bytes // 1024 // 1024}MB")
                continue
            sha = hashlib.sha256(raw).hexdigest()
            if sha in known_shas:
                dup.append(src.name)
                continue
            stored = f"{uuid.uuid4().hex}{src.suffix.lower()}"
            await asyncio.to_thread((self._img_dir / stored).write_bytes, raw)
            item = {
                "id": uuid.uuid4().hex,
                "stored_name": stored,
                "sha256": sha,
                "name": src.stem[:40],
                "tags": [],
                "emotions": [],
                "intents": [],
                "description": "",
                "visible_text": "",
                "enabled": True,
                "size": len(raw),
                "mime": mimetypes.guess_type(src.name)[0] or "image/jpeg",
                "analysis_status": "none",
                "analysis_error": "",
                "source": "import_dir",
                "created_at": _now(),
            }
            self._items.append(item)
            known_shas.add(sha)
            saved.append(item)
            if auto_tag and self._queue_analysis(item):
                queued += 1
        if saved:
            self._save_index(immediate=True)
        msg = {
            "saved": len(saved),
            "duplicated": [d for d in dup[:10]],
            "duplicated_total": len(dup),
            "rejected": rejected[:10],
            "rejected_total": len(rejected),
            "scanned": len(candidates),
            "queued": queued,
            "truncated": truncated,
        }
        return self._ok(msg)

    def _find(self, sticker_id: str) -> dict | None:
        return next((it for it in self._items if it["id"] == str(sticker_id)), None)

    async def _api_update(self):
        payload = await quart_request.get_json(silent=True) or {}
        item = self._find(payload.get("id") or "")
        if item is None:
            return self._err("找不到该表情包")
        # 只在值真的变了的时候标记「手改」，避免点一下保存就把整条锁成人工标注
        changed: set[str] = set()
        for field in ("name", "description", "visible_text"):
            if field in payload:
                new = str(payload[field] or "")[: 40 if field == "name" else 200]
                if new != str(item.get(field) or ""):
                    item[field] = new
                    changed.add(field)
        for field in ("tags", "emotions", "intents"):
            if field in payload:
                cap = 6 if field == "tags" else 8
                new = [str(t).strip()[:12] for t in (payload[field] or []) if str(t).strip()][:cap]
                if new != [str(t) for t in (item.get(field) or [])]:
                    item[field] = new
                    changed.add(field)
        if "enabled" in payload:
            item["enabled"] = bool(payload["enabled"])
        if changed:
            human = set(item.get("human") or [])
            human |= changed
            item["human"] = sorted(human)
            # 人工接管了标注，就算打标没跑过也算完成，否则面板一直显示「未打标」
            if item.get("analysis_status") in (None, "", "none") and item.get("emotions"):
                item["analysis_status"] = "complete"
        self._save_index(immediate=True)
        return self._ok({"human": item.get("human") or []})

    async def _purge_items(self, targets: list[dict]) -> int:
        """从库存里彻底移除若干条（图片文件 + 索引），返回真正删掉的数量。"""
        removed = 0
        for item in targets:
            iid = item["id"]
            # worker 池化之后没法单独取消一张在飞的请求：从队列里撤掉待处理的，
            # 正在跑的那张等它自然结束，保存时它已经不在 _items 里，不会复活。
            self._queued_ids.discard(iid)
            try:
                await asyncio.to_thread((self._img_dir / item["stored_name"]).unlink, True)
            except Exception:
                pass
            try:
                self._items.remove(item)
            except ValueError:
                continue
            removed += 1
        if removed:
            self._save_index(immediate=True)
        return removed

    async def _api_delete(self):
        payload = await quart_request.get_json(silent=True) or {}
        item = self._find(payload.get("id") or "")
        if item is None:
            return self._err("找不到该表情包")
        await self._purge_items([item])
        return self._ok()

    async def _api_delete_batch(self):
        """批量删除：一次请求、一次写索引，比前端循环打 /delete 快得多也不容易半途失败。"""
        payload = await quart_request.get_json(silent=True) or {}
        raw = payload.get("ids")
        if not isinstance(raw, list):
            return self._err("参数错误：ids 需要是数组")
        want = {str(i).strip() for i in raw if str(i).strip()}
        if not want:
            return self._err("没有选择要删除的表情包")
        if len(want) > 5000:
            return self._err("一次最多删 5000 张")
        targets = [it for it in self._items if it["id"] in want]
        if not targets:
            return self._err("这些表情包都不在库里了（可能已被别处删掉）")
        removed = await self._purge_items(targets)
        return self._ok({"deleted": removed, "missing": max(0, len(want) - removed)})

    async def _api_review(self):
        pend = [it for it in self._items if _review_state(it) == "pending"]
        pend.sort(key=lambda x: -float(x.get("created_at") or 0))
        return self._ok({
            "items": [self._item_view(it) for it in pend],
            "count": len(pend),
            "rejected": sum(1 for it in self._items if _review_state(it) == "rejected"),
            "steal_on": self._steal_on(),
            "auto": self._steal_auto(),
        })

    async def _api_review_apply(self):
        """审核偷来的图。scope=all 表示「全部待审核」，否则按 ids 挑。"""
        payload = await quart_request.get_json(silent=True) or {}
        action = str(payload.get("action") or "").strip().lower()
        if action not in ("allow", "reject", "delete"):
            return self._err("未知操作：只支持 allow / reject / delete")
        if str(payload.get("scope") or "") == "all":
            targets = [it for it in self._items if _review_state(it) == "pending"]
        else:
            raw = payload.get("ids")
            if not isinstance(raw, list):
                return self._err("参数错误：ids 需要是数组")
            want = {str(i).strip() for i in raw if str(i).strip()}
            if not want:
                return self._err("没有选中任何表情包")
            targets = [
                it for it in self._items
                if it["id"] in want and _review_state(it) == "pending"
            ]
        if not targets:
            return self._err("没有待审核的表情包（可能已经处理过了）")

        if action == "delete":
            removed = await self._purge_items(targets)
            return self._ok({"deleted": removed, "count": len(targets)})

        if action == "reject":
            # 拒绝 = 直接从库里删掉。它本来就不参与检索，留着只是占地方，
            # 下次翻列表还得再看一遍。图片文件、索引记录、待打标队列一并清掉。
            removed = await self._purge_items(targets)
            return self._ok({"rejected": removed, "deleted": removed, "count": len(targets)})

        # allow：通过之后才补打标 —— 待审核阶段不打标是为了不白花 token
        queued = 0
        for it in targets:
            it["review"] = "approved"
            it["reviewed_at"] = _now()
        if bool(self.config.get("auto_tag_on_upload", True)):
            for it in targets:
                if (it.get("analysis_status") or "none") == "none" and self._queue_analysis(it):
                    queued += 1
        self._save_index(immediate=True)
        return self._ok({"allowed": len(targets), "queued": queued, "count": len(targets)})

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
        if not self._llm_ready():
            return self._err(
                "打标模型还没配好：去「插件配置 → LLM 视觉打标」填 API，"
                "或选一个 AstrBot 提供商（再勾几个候选模型更抗限流）"
            )
        force = bool(payload.get("force"))
        if not self._queue_analysis(item, force=force):
            return self._err("这张图已经在打标队列里了")
        return self._ok({"queued": True, "force": force, "kept": item.get("human") or []})

    # ---------- 打标队列面板 ----------

    async def _api_queue(self):
        now = _now()
        active, queued, failed = [], [], []
        for it in self._items:
            iid = it.get("id")
            st = it.get("analysis_status") or "none"
            if iid in self._active_ids:
                active.append({
                    "id": iid,
                    "name": it.get("name") or "",
                    "elapsed": round(now - self._active_since.get(iid, now), 1),
                })
            elif st == "pending":
                waited = self._queued_at.get(iid)
                queued.append({
                    "id": iid,
                    "name": it.get("name") or "",
                    "waited": round(now - waited, 1) if waited else 0,
                })
            elif st == "failed":
                failed.append({
                    "id": iid,
                    "name": it.get("name") or "",
                    "err": (it.get("analysis_error") or "").strip()[:160],
                })
        queued.sort(key=lambda x: x["waited"], reverse=True)  # 等得久的排前面
        return self._ok({
            "stats": self._queue_stats(),
            "active": active,
            "queued": queued[:200],
            "queued_total": len(queued),
            "failed": failed[:100],
            "failed_total": len(failed),
            "llm": self._llm_source_desc(),
            "llm_ready": self._llm_ready(),
            # 前端「打标队列」页显示用：主模型 + 候选，down>0 表示这个模型正在限流冷却
            "targets": [
                {
                    "model": t["model"],
                    "label": t["label"],
                    "down": round(max(0.0, self._prov_down.get(t["pk"], 0.0) - now), 0),
                }
                for t in self._llm_targets()
            ],
        })

    async def _api_queue_pause(self):
        payload = await quart_request.get_json(silent=True) or {}
        want = payload.get("paused")
        if want is None:
            want = not self._queue_paused
        self._set_paused(bool(want))
        return self._ok({"paused": self._queue_paused, "stats": self._queue_stats()})

    async def _api_queue_clear(self):
        dropped = self._drop_queued()
        return self._ok({"dropped": dropped, "stats": self._queue_stats()})

    async def _api_queue_retry(self):
        """批量重新排队：scope=failed（默认）或 none（未打标）。"""
        if not self._llm_ready():
            return self._err("请先在插件配置里填好打标模型，或选一个 AstrBot 提供商")
        payload = await quart_request.get_json(silent=True) or {}
        scope = str(payload.get("scope") or "failed")
        targets = [it for it in self._items if (it.get("analysis_status") or "none") == scope]
        queued = sum(1 for it in targets if self._queue_analysis(it))
        return self._ok({"queued": queued, "total": len(targets),
                         "stats": self._queue_stats()})

    # ---------- 生命周期 ----------

    async def initialize(self) -> None:
        """插件激活：续跑上次被打断的打标任务，并预热 worker 池。"""
        pending = [it for it in self._items if it.get("analysis_status") == "pending"]
        if not pending:
            return
        if not self._llm_ready():
            for it in pending:
                it["analysis_status"] = "none"
            self._save_index(immediate=True)
            logger.info("[StickerKit] LLM 未配置，已把 %d 条 pending 重置为未打标", len(pending))
            return
        logger.info(f"[StickerKit] 续跑上次中断的打标任务：{len(pending)} 张")
        queued = sum(1 for it in pending if self._queue_analysis(it))
        logger.info(f"[StickerKit] 已重新入队 {queued} 张")

    async def terminate(self):
        for w in self._workers:
            if not w.done():
                w.cancel()
        self._workers = []
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
        self._save_index(immediate=True)
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None
