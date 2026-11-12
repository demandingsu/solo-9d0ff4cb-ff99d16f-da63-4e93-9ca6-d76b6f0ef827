"""按项目编号归并公告，按发布时间处理投标截止时间。

规则（需求口径）：
1. 同一项目编号下的公告按发布时间从早到晚处理。
2. 公告明确写出新的投标截止时间时，用新值替换旧值。
3. 较晚的更正/延期公告声明延期，却没有给出新截止时间时，
   结果标记为"待核实"，且不再展示/沿用旧截止时间。
4. 从未出现过截止时间的项目标记为"未公布"。
5. 字段无法确认来自当前公告正文（如侧栏旧公告混入、正文同一字段多个取值）时，
   该项目（或该页）标记为"未核定"，不提供任何可用的截止时间，并保留问题来源；
   未核定的公告不参与截止时间替换，也不会用其字段做跨项目归并。
"""
from __future__ import annotations

import re

from .models import Announcement, ProjectResult, ResultStatus
from .utils import parse_time

_CORRECTION_TYPE_RE = re.compile(r"更正|变更|澄清|修改|延期|顺延|补充")
_UNVERIFIED_KEY_PREFIX = "__unverified__:"


def _is_correction(ann: Announcement) -> bool:
    return bool(_CORRECTION_TYPE_RE.search(ann.notice_type)) or ann.declares_extension


def _extract_project_name(ann: Announcement) -> str:
    """从公告标题中尽量取出项目名称。"""
    if not ann.title:
        return ""
    name = re.sub(r"[-_—–|].{0,4}(?:招标|采购|更正|变更|澄清|修改|延期|顺延|中标|成交|终止).{0,4}公告$", "", ann.title)
    name = re.sub(r"^(?:.*?关于)?(.*?)(?:项目)?(?:的)?(?:招标|采购|更正|变更|澄清|修改|延期|顺延|中标|成交|终止|废标)公告.*$", r"\1", name)
    return name.strip("-_—–| ")


def _group_key(ann: Announcement) -> str:
    """未核定且无法确定项目编号的页面单独成组，避免跨项目误归并。"""
    if ann.unverified and not ann.project_no:
        if ann.candidate_project_no:
            # 用侧栏候选编号命名分组，便于对照；但该页仍不参与任何有效期限计算
            return f"{_UNVERIFIED_KEY_PREFIX}{ann.candidate_project_no}:{ann.url}"
        return f"{_UNVERIFIED_KEY_PREFIX}{ann.url}"
    return ann.project_no


def merge_announcements(announcements: list[Announcement]) -> list[ProjectResult]:
    grouped: dict[str, list[Announcement]] = {}
    for ann in announcements:
        grouped.setdefault(_group_key(ann), []).append(ann)

    results: list[ProjectResult] = []
    for key, items in grouped.items():
        # 按发布时间升序；解析不出时间的排到最后，保证"较晚公告"判定稳定
        items.sort(key=lambda a: (parse_time(a.publish_time) is None, parse_time(a.publish_time) or 0))

        current_deadline: str | None = None
        pending = False
        pending_note = ""
        issue_sources: list[str] = []

        for ann in items:
            if ann.unverified:
                # 字段归属不明：该页字段不参与任何期限判定，所在项目本轮整体不核定
                issue_sources.extend(ann.issue_sources or [ann.unverified_reason])
                continue
            if ann.deadline:
                # 明确给出新截止时间 -> 替换旧值，并解除待核实
                current_deadline = ann.deadline
                pending = False
                pending_note = ""
            elif ann.declares_extension and _is_correction(ann):
                # 声明延期却没有新时间 -> 待核实，旧时间失效
                current_deadline = None
                pending = True
                pending_note = (
                    f"较晚公告（{ann.notice_type or '更正公告'}，{ann.publish_time or '发布时间未知'}）"
                    "声明延期但未写明新的投标截止时间，旧时间不再视为有效，需人工核实。"
                )

        project_no = items[0].project_no or items[0].candidate_project_no or "未能识别"
        project_name = _extract_project_name(items[-1]) or items[-1].title

        if issue_sources:
            status = ResultStatus.UNVERIFIED
            note = "本轮未核定：无法确认字段属于当前公告，故不提供可用的投标截止时间，问题来源见下。"
            current_deadline = None
        elif pending:
            status = ResultStatus.PENDING
            note = pending_note
        elif current_deadline:
            status = ResultStatus.EFFECTIVE
            note = "当前截止时间取自最新一条写明截止时间的公告。"
        else:
            status = ResultStatus.UNKNOWN
            note = "已抓取公告中均未公布投标截止时间。"

        results.append(
            ProjectResult(
                project_no=project_no or "未能识别",
                project_name=project_name,
                status=status,
                current_deadline=current_deadline,
                note=note,
                announcements=items,
                issue_sources=sorted(set(issue_sources)),
                candidate_project_no=items[0].candidate_project_no,
                project_no_is_candidate=bool(items[0].candidate_project_no and not items[0].project_no),
            )
        )

    # 项目编号排序，保证展示稳定；未核定的独立页面排在最后
    results.sort(key=lambda r: (r.project_no == "未能识别", r.status == ResultStatus.UNVERIFIED,
                                r.project_no, r.announcements[0].url if r.announcements else ""))
    return results
