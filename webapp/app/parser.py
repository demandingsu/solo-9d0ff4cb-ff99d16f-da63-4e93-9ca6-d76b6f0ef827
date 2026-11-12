"""详情页解析：按字段标签提取项目编号、公告类型、发布时间、投标截止时间。

解析不依赖固定页面结构，按"字段标签 -> 值"的思路依次尝试：
1. 表格行（th/td、tr 内含两个单元格）
2. dl/dt/dd 定义列表
3. 带 label 的表单项（label + input/textarea）
4. 形如「字段标签：值」的正文段落 / div

字段归属规则：
- 侧栏 / 相关公告（aside、related、sidebar 等区域）里的字段一律不参与解析，
  侧栏只提供旧公告的跳转链接，不能覆盖或补充当前公告正文的字段；
- 同一字段在正文中出现多个不同取值、或项目编号只能从侧栏取得时，
  无法确认字段属于哪一条公告，整页标记为"未核定"并保留问题来源，
  不再输出可用的投标截止时间；
- 原有表格与 dl/dt/dd 详情仍按字段标签正常采集。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

from .models import Announcement
from .utils import parse_time, strip_label

# 字段标签别名 -> 规范键
_LABEL_ALIASES: dict[str, str] = {
    "项目编号": "project_no",
    "招标项目编号": "project_no",
    "采购项目编号": "project_no",
    "项目编码": "project_no",
    "公告类型": "notice_type",
    "类型": "notice_type",
    "发布时间": "publish_time",
    "公告发布时间": "publish_time",
    "发布日期": "publish_time",
    "投标截止时间": "deadline",
    "投标截止日期": "deadline",
    "开标时间": "deadline",
    "投标文件递交截止时间": "deadline",
    "截止时间": "deadline",
    "更正后投标截止时间": "deadline",
    "延期后投标截止时间": "deadline",
    "新的投标截止时间": "deadline",
}

# 明确表示"更正后的现行值"的标签，优先于通用的"投标截止时间"
_PREFERRED_VALUE_LABELS = {"更正后投标截止时间", "延期后投标截止时间", "新的投标截止时间"}

# 侧栏 / 相关公告区域的识别（class、id 或标签名）
_EN_SIDE_TOKENS = {
    "sidebar", "side", "aside", "related", "recommend", "recommendation",
    "hot", "rank", "ranking", "history", "archive", "navigation", "navbar",
    "breadcrumb", "footer", "menu",
}
_CN_SIDE_HINTS = (
    "相关公告", "相关推荐", "相关项目", "相关链接", "热门", "推荐",
    "侧栏", "侧边", "导航", "页脚", "历史公告", "往期公告",
)

# 标题里出现这些字样时，说明该标题不是当前公告标题（多见于侧栏标题）
_GENERIC_TITLE_RE = re.compile(r"^(?:相关公告|相关推荐|相关项目|相关链接|热门公告|历史公告|往期公告)$")

# 兜底正则
_PROJECT_NO_RE = re.compile(r"(?:项目编号|项目编码)[：:\s]*([0-9A-Za-z][0-9A-Za-z\-_/（）()\.]{2,40})")
_PUBLISH_RE = re.compile(r"发布(?:时间|日期)[：:\s]*([0-9]{4}[年/\-.][0-9]{1,2}[月/\-.][0-9]{1,2}日?(?:\s*[0-9]{1,2}[:：][0-9]{2}(?:[:：][0-9]{2})?)?)")
_DEADLINE_RE = re.compile(r"(?:投标|递交)?(?:文件)?截止(?:时间|日期)[：:\s]*([0-9]{4}[年/\-.][0-9]{1,2}[月/\-.][0-9]{1,2}日?(?:\s*[0-9]{1,2}[:：][0-9]{2}(?:[:：][0-9]{2})?)?)")
_TYPE_RE = re.compile(r"公告类型[：:\s]*([^\s<，,。；;]{2,12}公告)")

# 声明延期的正文特征
_EXTENSION_PATTERNS = (
    re.compile(r"(?:投标|开标|响应|递交|报名).{0,12}(?:截止|时间).{0,10}(?:延期|顺延|推迟|延后)"),
    re.compile(r"(?:延期|顺延|推迟|延后).{0,12}(?:投标|开标|响应|递交|截止)"),
    re.compile(r"(?:延期|顺延|推迟).{0,6}(?:公告|通知)"),
)

_FIELD_LABELS = {
    "project_no": "项目编号",
    "notice_type": "公告类型",
    "publish_time": "发布时间",
    "deadline": "投标截止时间",
}


@dataclass
class _FieldPair:
    """一个从页面结构中读到的字段标签/值对，附带来源说明。"""

    key: str
    label: str
    value: str
    origin: str


def _map_label(label_text: str) -> str | None:
    key = strip_label(label_text)
    return _LABEL_ALIASES.get(key)


def _origin_text(tag: Tag) -> str:
    """给字段来源一个可读描述（用于"未核定"的问题来源）。"""
    classes = " ".join(str(c) for c in tag.get("class", []))
    tag_id = tag.get("id", "")
    detail = " ".join(part for part in (classes, str(tag_id)) if part)
    return f"<{tag.name} {detail}>" if detail else f"<{tag.name}>"


def _is_side_tag(tag: Tag) -> bool:
    if not isinstance(tag, Tag) or tag.name in ("html", "body", "head"):
        return False
    if tag.name == "aside":
        return True
    classes = [str(c).strip().lower() for c in tag.get("class", [])]
    tag_id = str(tag.get("id", "")).strip().lower()
    tokens = {token for token in classes + [tag_id] if token}
    # 英文 class/id 按词切分（side-bar、related-list 等）
    words = {word for token in tokens for word in re.split(r"[-_ ]+", token) if word}
    if words & _EN_SIDE_TOKENS or tokens & _EN_SIDE_TOKENS:
        return True
    marker = " ".join(classes + [tag_id])
    return any(hint.lower() in marker for hint in _CN_SIDE_HINTS)


def _is_generic_heading(text: str) -> bool:
    return _GENERIC_TITLE_RE.match(text.strip()) is not None


def _find_side_nodes(soup: BeautifulSoup) -> list[Tag]:
    """找出顶层侧栏/相关公告区域；嵌套的侧栏只保留最外层。"""
    candidates = [tag for tag in soup.find_all(True) if _is_side_tag(tag)]
    side_ids = {id(tag) for tag in candidates}
    top_level: list[Tag] = []
    for tag in candidates:
        if any(id(parent) in side_ids for parent in tag.parents):
            continue
        top_level.append(tag)
    return top_level


@dataclass
class _Collected:
    pairs: list[_FieldPair] = field(default_factory=list)

    def add(self, label: str, value: str, origin: str) -> None:
        key = _map_label(label)
        if key and value and value.strip():
            self.pairs.append(_FieldPair(key=key, label=strip_label(label), value=value.strip(), origin=origin))


def _collect_pairs(root: Tag | BeautifulSoup) -> _Collected:
    """从给定区域收集 字段标签 -> 值。侧栏节点应已被移除。"""
    collected = _Collected()

    # 1. 表格：th+td，或一行两个 td
    for row in root.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) >= 2:
            collected.add(cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True),
                          f"表格行{_origin_text(row)}")
        if len(cells) >= 4:  # 一行两对字段
            collected.add(cells[2].get_text(" ", strip=True), cells[3].get_text(" ", strip=True),
                          f"表格行{_origin_text(row)}")

    # 2. dl/dt/dd（按 dt 后紧跟的 dd 配对）
    for dl in root.find_all("dl"):
        pending_dt: Tag | None = None
        for child in dl.find_all(["dt", "dd"], recursive=False):
            if child.name == "dt":
                pending_dt = child
            elif pending_dt is not None:
                collected.add(pending_dt.get_text(" ", strip=True), child.get_text(" ", strip=True),
                              f"定义列表{_origin_text(dl)}")
                pending_dt = None

    # 3. label + 输入控件
    for label in root.find_all("label"):
        target_id = label.get("for")
        control = None
        if target_id:
            control = root.find(attrs={"id": target_id}) if hasattr(root, "find") else None
            if control is None:
                control = label.find_next(attrs={"id": target_id})
        if control is None:
            control = label.find_next(["input", "textarea", "select"])
        if control is not None:
            value = control.get("value") if control.name == "input" else control.get_text(" ", strip=True)
            if value:
                collected.add(label.get_text(" ", strip=True), str(value), f"表单项{_origin_text(label)}")

    # 4. 正文文本中的「标签：值」
    for element in root.find_all(["p", "div", "li", "span"]):
        # 只取直接文本，避免父容器把子段落重复收进来；
        # 同一元素内的多个块级子节点（如侧栏字段 span）用换行分隔，避免相邻字段黏连
        parts: list[str] = []
        for child in element.children:
            if getattr(child, "name", None):
                direct = "".join(
                    str(text) for text in child.find_all(string=True, recursive=False)
                )
                parts.append("\n" + direct + "\n")
            else:
                parts.append(str(child))
        own_text = "".join(parts)
        for match in re.finditer(
            r"([一-龥A-Za-z]{2,12}?)\s*[：:]\s*([^\n\r<，,。；;]{1,60})",
            own_text,
        ):
            collected.add(match.group(1), match.group(2), f"正文段落{_origin_text(element)}")

    return collected


def _canonical_value(key: str, value: str) -> str:
    """用于判断多个来源是否为同一取值（忽略排版差异）。"""
    clean = strip_label(value).strip().strip("，,。；;、")
    if key in ("deadline", "publish_time"):
        parsed = parse_time(clean)
        if parsed is not None:
            return parsed.isoformat()
    return re.sub(r"\s+", "", clean).upper() if key == "project_no" else re.sub(r"\s+", " ", clean)


def _extract_title(soup: BeautifulSoup) -> str:
    title_tag = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag_name in ("h1", "h2", "h3"):
        for heading in soup.find_all(tag_name):
            text = heading.get_text(" ", strip=True)
            if len(text) >= 4 and not _is_generic_heading(text):
                return text
    return title_tag


def parse_detail(html: str, url: str) -> Announcement:
    """解析一个公告详情页，返回 Announcement。

    页面完全没有项目编号线索时抛出 ValueError，调用方据此跳过非公告页。
    正文与侧栏/同页多值无法分清字段归属时，返回 unverified=True 的公告，
    其字段不再作为当前公告的有效值使用。
    """
    soup = BeautifulSoup(html, "html.parser")

    # 先把侧栏/相关公告整块摘掉：其中的旧公告字段绝不能覆盖当前正文
    side_nodes = _find_side_nodes(soup)
    side_collected = _Collected()
    for node in side_nodes:
        for pair in _collect_pairs(node).pairs:
            side_collected.pairs.append(pair)
    for node in side_nodes:
        node.decompose()

    title = _extract_title(soup)
    main_collected = _collect_pairs(soup)
    main_text = soup.get_text(" ", strip=True)

    by_key: dict[str, list[_FieldPair]] = {}
    for pair in main_collected.pairs:
        by_key.setdefault(pair.key, []).append(pair)

    side_by_key: dict[str, list[_FieldPair]] = {}
    for pair in side_collected.pairs:
        side_by_key.setdefault(pair.key, []).append(pair)

    # 标签缺失时的正则兜底（只在正文文本中找，不看侧栏）
    fallbacks = {
        "project_no": _PROJECT_NO_RE,
        "publish_time": _PUBLISH_RE,
        "deadline": _DEADLINE_RE,
        "notice_type": _TYPE_RE,
    }
    for key, pattern in fallbacks.items():
        if key not in by_key:
            match = pattern.search(main_text)
            if match:
                by_key[key] = [_FieldPair(key=key, label=_FIELD_LABELS[key],
                                          value=re.sub(r"\s+", " ", match.group(1)).strip(),
                                          origin="正文文本正则兜底")]

    # 同一字段出现多个不同取值 -> 无法确认字段属于哪一条公告
    # 先只看当前正文；侧栏字段单独比对，绝不覆盖正文
    ambiguous: dict[str, list[_FieldPair]] = {}
    resolved: dict[str, str] = {}
    for key, values in by_key.items():
        # 正文同时给出“更正前/更正后”时，以明确标注为“更正后/延期后/新的”为准
        preferred = [pair for pair in values if strip_label(pair.label) in _PREFERRED_VALUE_LABELS]
        if preferred:
            values = preferred
        distinct: dict[str, _FieldPair] = {}
        for pair in values:
            distinct.setdefault(_canonical_value(key, pair.value), pair)
        if len(distinct) > 1:
            ambiguous[key] = list(distinct.values())
        else:
            resolved[key] = values[0].value.strip()

    issues: list[str] = []
    for key, values in ambiguous.items():
        shown = "、".join(f"{v.value}（{v.origin}）" for v in values)
        issues.append(
            f"{url}：正文中“{_FIELD_LABELS[key]}”出现多个不同取值：{shown}，"
            "无法确认哪些字段属于当前公告"
        )

    # 正文缺失、只能从侧栏/相关公告读到的字段：
    # - 项目编号是公告归属的关键，缺它就无法确认当前页属于哪个项目 -> 未核定；
    # - 其余字段（类型/发布时间/截止时间）侧栏值属于旧公告，直接忽略、不得补进正文，
    #   若正文本身声明延期但未给新时间，由归并阶段判为“待核实”。
    side_only: dict[str, list[_FieldPair]] = {}
    for key, side_values in side_by_key.items():
        if key in ambiguous or key in resolved:
            continue
        side_only[key] = side_values
    if "project_no" in side_only:
        shown = "、".join(v.value for v in side_only["project_no"])
        issues.append(
            f"{url}：正文未找到项目编号，仅在侧栏/相关公告区域出现项目编号（{shown}），"
            "无法确认当前公告归属"
        )

    project_no = resolved.get("project_no", "").strip()
    candidate_project_no = ""
    if not project_no and "project_no" not in ambiguous and "project_no" in side_only:
        # 正文缺编号：侧栏编号只作为候选，绝不参与归并，页面标记未核定
        candidate_project_no = side_only["project_no"][0].value.strip()
    if not project_no and "project_no" not in ambiguous and "project_no" not in side_only:
        raise ValueError(f"页面未找到项目编号字段，可能不是公告详情页: {url}")

    notice_type = resolved.get("notice_type", "")
    # 标题里的公告类型也可作为依据
    type_in_title = re.search(r"(招标|采购|更正|变更|澄清|修改|延期|顺延|中标|成交|终止|废标)公告", title)
    if not notice_type and type_in_title:
        notice_type = type_in_title.group(0)

    declares_extension = any(pattern.search(main_text) for pattern in _EXTENSION_PATTERNS)
    if re.search(r"延期|顺延", notice_type):
        declares_extension = True

    unverified = bool(ambiguous) or "project_no" in side_only
    # 字段归属不明时，不保留可能被误用的有效截止时间
    deadline = None if "deadline" in ambiguous else (resolved.get("deadline") or None)

    return Announcement(
        url=url,
        project_no=project_no,
        title=title,
        notice_type=notice_type,
        publish_time=resolved.get("publish_time", ""),
        deadline=deadline,
        declares_extension=declares_extension,
        unverified=unverified,
        unverified_reason="；".join(issues),
        issue_sources=issues,
        candidate_project_no=candidate_project_no,
    )
