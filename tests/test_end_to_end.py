"""端到端测试：启动本地静态站点 -> 爬虫抓取 -> 归并核对。

运行方式：
    PYTHONPATH=/tmp/pylibs python3 -I tests/test_end_to_end.py
"""
from __future__ import annotations

import functools
import http.server
import sys
import threading
from pathlib import Path

# -I 隔离模式下 PYTHONPATH 不生效，显式注入依赖目录
for candidate in ("/tmp/pylibs", "/tmp/pyroot/lib/python3.11/site-packages"):
    if Path(candidate).exists() and candidate not in sys.path:
        sys.path.insert(0, candidate)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webapp"))

from app.crawler import crawl  # noqa: E402
from app.merger import merge_announcements  # noqa: E402
from app.models import ResultStatus  # noqa: E402
from app.parser import parse_detail  # noqa: E402

SITE_DIR = Path(__file__).resolve().parent.parent / "example-site" / "site"
EXPECTED = {
    "CG-2026-001": (ResultStatus.EFFECTIVE, "2026-11-06 10:00:00", 2),
    "CG-2026-002": (ResultStatus.EFFECTIVE, "2026-10-28 15:00:00", 2),
    "CG-2026-003": (ResultStatus.PENDING, None, 2),
    "CG-2026-004": (ResultStatus.PENDING, None, 3),
    "CG-2026-005": (ResultStatus.EFFECTIVE, "2026-11-02 09:30:00", 1),
    "CG-2026-006": (ResultStatus.UNKNOWN, None, 1),
    # P7：延期无新时间，侧栏旧公告的旧截止时间不得变成有效值
    "CG-2026-007": (ResultStatus.PENDING, None, 2),
    # P8：更正页侧栏是另一项目的旧公告，不得跨项目归并，也不得覆盖新时间
    "CG-2026-008": (ResultStatus.EFFECTIVE, "2026-11-12 10:00:00", 2),
    "CG-2026-009": (ResultStatus.EFFECTIVE, "2026-10-28 10:00:00", 1),
    # P8/P9 涉及的页面内字段冲突场景在 [6][7] 中单独断言
    "CG-2026-010": (ResultStatus.UNVERIFIED, None, 1),
    "CG-2026-011": (ResultStatus.UNVERIFIED, None, 1),
}


def start_server() -> str:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}/list/page_1.html"


def assert_equal(actual, expected, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}：期望 {expected!r}，实际 {actual!r}")
    print(f"  ✓ {message}")


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"{message}：条件不成立")
    print(f"  ✓ {message}")


def main() -> int:
    entry = start_server()
    print(f"[1] 本地示例站点：{entry}")

    print("[2] 从列表页沿下一页抓取并解析详情 ...")
    report = crawl(entry)
    assert_equal(len(report.visited_list_pages), 2, "遍历了 2 个列表页（下一页跟随成功）")
    assert_equal(len(report.announcements), 19, "解析出 19 条公告")
    assert_equal(report.errors, [], "抓取过程无错误")

    # 抽查一条详情页的字段标签解析
    first = next(a for a in report.announcements if a.project_no == "CG-2026-001" and a.notice_type == "更正公告")
    assert_equal(first.publish_time, "2026-10-15 16:20:00", "更正公告发布时间按标签正确解析")
    assert_equal(first.deadline, "2026-11-06 10:00:00", "更正公告新截止时间按标签正确解析")

    pending_notice = next(a for a in report.announcements if a.project_no == "CG-2026-003" and a.notice_type == "延期公告")
    assert_equal(pending_notice.deadline, None, "无新时间的延期页截止时间为 None")
    assert_equal(pending_notice.declares_extension, True, "延期页被识别为声明延期")

    print("[3] 按项目编号归并并按发布时间处理 ...")
    results = merge_announcements(report.announcements)
    assert_equal(len(results), 12, "归并出 12 条项目/页面记录")
    by_no = {r.project_no: r for r in results}

    for project_no, (status, deadline, count) in EXPECTED.items():
        result = by_no[project_no]
        assert_equal(result.status, status, f"{project_no} 状态为 {status.value}")
        assert_equal(result.current_deadline, deadline,
                     f"{project_no} 当前截止时间为 {deadline or '空（不显示旧值）'}")
        assert_equal(len(result.announcements), count, f"{project_no} 保留 {count} 条公告链接")
        # 公告按发布时间升序
        times = [a.publish_time for a in result.announcements]
        assert_equal(times, sorted(times), f"{project_no} 公告按发布时间升序排列")

    # 正文缺项目编号的页面试图用侧栏候选编号命名，但不得与真实项目合并
    ambiguous_entries = [r for r in results if r.project_no == "CG-2026-010"]
    assert_equal(len(ambiguous_entries), 2, "CG-2026-010 的正文页与侧栏候选页分属两条记录")
    page_only = next(r for r in ambiguous_entries if r.candidate_project_no)
    assert_equal(page_only.status, ResultStatus.UNVERIFIED, "仅侧栏出现编号的页面为未核定")
    assert_equal(page_only.current_deadline, None, "未核定页面不提供可用期限")
    assert_true(any("侧栏" in issue for issue in page_only.issue_sources), "未核定记录保留问题来源")

    print("[4] 解析器兜底能力（dl/dt/dd 与纯文本标签）...")
    alt_html = """
    <html><body><h1>某设备采购更正公告</h1>
      <dl><dt>项目编号：</dt><dd>ALT-999</dd>
          <dt>公告类型</dt><dd>更正公告</dd>
          <dt>发布时间：</dt><dd>2026年11月01日 10:00</dd></dl>
      <p>投标截止时间：2026年11月20日 09:30，特此通知。</p>
    </body></html>
    """
    ann = parse_detail(alt_html, "http://test/alt.html")
    assert_equal(ann.project_no, "ALT-999", "dl/dt/dd 结构解析项目编号")
    assert_equal(ann.notice_type, "更正公告", "dl/dt/dd 结构解析公告类型")
    assert_equal(ann.publish_time, "2026年11月01日 10:00", "dl/dt/dd 结构解析发布时间")
    assert_equal(ann.deadline, "2026年11月20日 09:30", "纯文本标签兜底解析截止时间")

    print("[5] 侧栏与正文冲突的解析规则 ...")
    # 侧栏在前、且带旧公告字段：正文表格字段必须胜出，侧栏不得覆盖
    sidebar_first = """
    <html><body>
      <aside class="related">
        <h3>相关公告</h3>
        <ul><li><a href="/detail/old.html">旧项目招标公告</a>
          <div class="related-fields">
            <span>项目编号：CG-OLD-900</span><span>公告类型：招标公告</span>
            <span>发布时间：2026-10-01 09:00:00</span><span>投标截止时间：2026-10-20 09:00:00</span>
          </div></li></ul>
      </aside>
      <h1>市政道路养护设备采购更正公告</h1>
      <table class="fields">
        <tr><th>项目编号</th><td>CG-CUR-901</td></tr>
        <tr><th>公告类型</th><td>更正公告</td></tr>
        <tr><th>发布时间</th><td>2026-10-15 16:20:00</td></tr>
        <tr><th>投标截止时间</th><td>2026-11-06 10:00:00</td></tr>
      </table>
    </body></html>
    """
    ann = parse_detail(sidebar_first, "http://test/current.html")
    assert_equal(ann.project_no, "CG-CUR-901", "侧栏旧公告编号不覆盖正文项目编号")
    assert_equal(ann.notice_type, "更正公告", "侧栏旧公告类型不覆盖正文公告类型")
    assert_equal(ann.publish_time, "2026-10-15 16:20:00", "侧栏旧发布时间不覆盖正文发布时间")
    assert_equal(ann.deadline, "2026-11-06 10:00:00", "侧栏旧截止时间不覆盖正文截止时间")
    assert_equal(ann.title, "市政道路养护设备采购更正公告", "标题取正文标题而非侧栏标题")
    assert_equal(ann.unverified, False, "正文字段齐全时不属于未核定")

    # 正文同一字段两个取值：无法分清归属 -> 未核定，不提供有效期
    conflicting_html = """
    <html><body><h1>某项目更正公告</h1>
      <table><tr><th>项目编号</th><td>CG-CONFLICT-902</td></tr>
             <tr><th>投标截止时间</th><td>2026-11-05 09:00:00</td></tr></table>
      <p>投标截止时间：2026-10-29 09:00:00，请以本页通知为准。</p>
    </body></html>
    """
    conflict = parse_detail(conflicting_html, "http://test/conflict.html")
    assert_equal(conflict.unverified, True, "正文同一字段多值时标记未核定")
    assert_equal(conflict.deadline, None, "未核定页面不给出可用截止时间")
    assert_true(bool(conflict.issue_sources), "未核定页面保留问题来源")
    merged = merge_announcements([conflict])[0]
    assert_equal(merged.status, ResultStatus.UNVERIFIED, "未核定的公告归并后状态为未核定")
    assert_equal(merged.current_deadline, None, "未核定项目不提供有效期限")

    # 仅侧栏出现项目编号：不能据此归并，标为未核定
    side_only_html = """
    <html><body>
      <div class="sidebar"><span>项目编号：CG-SIDE-903</span>
        <span>投标截止时间：2026-10-24 09:00:00</span></div>
      <h1>社区卫生服务中心改造采购更正公告</h1>
      <p>投标截止时间调整为本公告所载时间。</p>
    </body></html>
    """
    side_only = parse_detail(side_only_html, "http://test/side-only.html")
    assert_equal(side_only.project_no, "", "侧栏编号不写入正文项目编号")
    assert_equal(side_only.unverified, True, "仅侧栏有编号时标记未核定")
    assert_equal(side_only.deadline, None, "侧栏截止时间不被当作当前公告期限")

    print("[6] 无项目编号的页面应被拒绝 ...")
    try:
        parse_detail("<html><body><p>没有字段标签的页面</p></body></html>", "http://test/x.html")
    except ValueError:
        print("  ✓ 无项目编号页面抛出 ValueError")
    else:
        raise AssertionError("无项目编号页面未被拒绝")

    print("\n全部测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
