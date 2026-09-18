"""报告 HTML 导出：独立单文件版本（内联样式），便于分享与打印。"""
from datetime import datetime
from pathlib import Path

import markdown

_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} · 研究报告</title>
<style>
  body {{ font-family: "Microsoft YaHei", "Segoe UI", sans-serif; color: #2c3e4a;
         max-width: 860px; margin: 32px auto; padding: 0 20px; line-height: 1.7; }}
  h1 {{ color: #1a2d38; font-size: 24px; border-bottom: 3px solid #667eea; padding-bottom: 8px; }}
  h2 {{ color: #1a2d38; font-size: 18px; border-left: 4px solid #667eea; padding-left: 10px; margin-top: 28px; }}
  a {{ color: #667eea; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #e4e8f2; padding: 6px 10px; font-size: 14px; }}
  code, pre {{ background: #f7f8fc; border-radius: 6px; padding: 2px 6px; font-size: 13px; }}
  .meta {{ color: #6b8592; font-size: 13px; margin-bottom: 20px; }}
  footer {{ margin-top: 40px; color: #9aa6c8; font-size: 12px; text-align: center; }}
  @media print {{ body {{ margin: 0; }} }}
</style>
</head>
<body>
{body}
<footer>由 智能研究助手 Agent 生成 · {time}</footer>
</body>
</html>
"""


def html_sibling(md_path) -> Path:
    """Markdown 报告对应的 HTML 文件路径（删除任务、导出、前端下载都依赖这个约定）。"""
    return Path(md_path).with_suffix(".html")


def export_html(report_md: str, md_path: Path, topic: str) -> Path:
    body = markdown.markdown(report_md, extensions=["tables", "fenced_code"])
    html = _TEMPLATE.format(title=topic, body=body,
                            time=datetime.now().strftime("%Y-%m-%d %H:%M"))
    out = html_sibling(md_path)
    out.write_text(html, encoding="utf-8")
    return out
