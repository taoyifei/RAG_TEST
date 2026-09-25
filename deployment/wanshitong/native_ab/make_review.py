#!/usr/bin/env python3
# ruff: noqa: E501 - 离线评审页的内嵌 HTML/CSS/JS 保持原格式。
"""从私有原始 SSE 生成匿名人工评审包，不把业务题面写入仓库。"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import random
import re
from pathlib import Path
from typing import Any

from replay_ab import _event_objects
from smoke_ab import _write_new

KB_TAG = re.compile(r"<kb\b[^>]*?/?>", re.IGNORECASE)
CHUNK_ID = re.compile(r'\bchunk_id="([^"]+)"', re.IGNORECASE)
BLIND_SEED = 9252026
GRADES = [
    "supported_correct_complete",
    "correct_but_unattributed",
    "partial",
    "appropriate_insufficiency_or_clarification",
    "unnecessary_refusal",
    "unsupported_or_wrong",
    "execution_failure",
]


def _source_lookup(path: Path) -> dict[str, dict[str, Any]]:
    """用原文定位键匹配受限摘录，不复制完整 DOCX。"""
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        key = f"private-sources/{item['document_id']}/{item['locator']}"
        if (
            hashlib.sha256(item["text"].encode()).hexdigest()
            != item["excerpt_sha256"]
        ):
            raise ValueError(f"原文摘录哈希不匹配：{key}")
        result[key] = item
    return result


def _a_answer(raw: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    """区分 A 最终可见正文与未发布草稿。"""
    events = _event_objects(raw)
    final = next((x for x in reversed(events) if x.get("type") == "final"), {})
    citations = final.get("citations") or []
    answer = final.get("answer") or ""
    cards = []
    for index, cite in enumerate(citations, 1):
        alias = cite.get("alias")
        if alias:
            answer = answer.replace(str(alias), f"[R{index}]")
        cards.append(
            {
                "label": f"R{index}",
                "document": cite.get("document_title")
                or cite.get("document_name"),
                "locator": cite.get("locator"),
                "excerpt": cite.get("quote"),
            }
        )
    draft = "".join(
        str(event.get("text") or "")
        for event in events
        if event.get("type") == "answer_delta"
    )
    status = (
        "已发布"
        if final.get("published")
        else "未发布"
        if final
        else "执行失败"
    )
    return (
        {
            "answer": answer,
            "display_status": status,
            "published": bool(final.get("published")),
            "citations": cards,
        },
        {
            "draft": draft,
            "raw_status": final.get("status"),
            "citation_status": final.get("citation_status"),
            "trace_id": final.get("trace_id"),
        },
    )


def _b_answer(raw: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    """只把 B 正文内引用当引用；预先发出的检索结果单列。"""
    events = _event_objects(raw)
    complete = next(
        (x for x in reversed(events) if x.get("response_type") == "complete"),
        {},
    )
    data = (
        complete.get("data") if isinstance(complete.get("data"), dict) else {}
    )
    answer = str(data.get("final_content") or "")
    ref_event = next(
        (x for x in events if x.get("response_type") == "references"),
        {},
    )
    retrieved = ref_event.get("knowledge_references") or []
    cards = []
    known: dict[str, int] = {}

    def replace_tag(match: re.Match[str]) -> str:
        id_match = CHUNK_ID.search(match.group(0))
        if id_match is None:
            return ""
        chunk_id = html.unescape(id_match.group(1))
        if chunk_id not in known:
            index = len(cards) + 1
            known[chunk_id] = index
            source = next(
                (
                    item
                    for item in retrieved
                    if chunk_id
                    in {
                        item.get("id"),
                        item.get("sub_chunk_id"),
                        item.get("parent_chunk_id"),
                    }
                ),
                {},
            )
            cards.append(
                {
                    "label": f"R{index}",
                    "document": source.get("knowledge_title")
                    or source.get("knowledge_filename"),
                    "locator": source.get("chunk_index"),
                    "excerpt": source.get("content"),
                    "source_matched": bool(source),
                }
            )
        return f"[R{known[chunk_id]}]"

    answer = KB_TAG.sub(replace_tag, answer)
    return (
        {
            "answer": answer,
            "display_status": "已发布" if complete else "执行失败",
            "published": bool(complete),
            "citations": cards,
        },
        {
            "raw_status": "complete" if complete else None,
            "retrieved_candidates": retrieved,
        },
    )


def _turn_result(
    run_dirs: list[Path], case_id: str, turn_index: int, side: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    filename = f"{case_id}-T{turn_index}-{side}.sse"
    paths = [
        folder / filename
        for folder in run_dirs
        if (folder / filename).is_file()
    ]
    if len(paths) != 1:
        return (
            {
                "answer": "",
                "display_status": "执行失败",
                "published": False,
                "citations": [],
            },
            {"raw_file_count": len(paths)},
        )
    raw = paths[0].read_bytes()
    return _a_answer(raw) if side == "A" else _b_answer(raw)


def _render_html(
    cases: list[dict[str, Any]],
    *,
    title: str,
    headings: dict[str, str],
    score_filename: str,
) -> str:
    """生成无外链的本地评审页，正文经 textContent 显示。"""
    payload = json.dumps(cases, ensure_ascii=False).replace("<", "\\u003c")
    grades = json.dumps(GRADES, ensure_ascii=False)
    heading_data = json.dumps(headings, ensure_ascii=False)
    template = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
body{font:16px/1.55 system-ui,sans-serif;max-width:1300px;margin:24px auto;padding:0 20px;color:#202939}
header{display:flex;gap:12px;align-items:center;flex-wrap:wrap;position:sticky;top:0;background:#fff;padding:10px 0;border-bottom:1px solid #ccd4dd}
button,select,textarea{font:inherit}button{padding:6px 12px}main{margin-top:24px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.arm{border:1px solid #ccd4dd;border-radius:8px;padding:16px;min-width:0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}.source{background:#f3f6fa;padding:10px;margin:8px 0;border-radius:6px}
.gold{background:#fff9e8;padding:16px;border-radius:8px;margin:20px 0}.muted{color:#5d6775}
textarea{width:100%;min-height:70px}select{max-width:100%;width:100%}@media(max-width:800px){.grid{grid-template-columns:1fr}}
</style></head><body>
<header><strong>__TITLE__</strong><span id="progress"></span><button id="prev">上一场景</button><button id="next">下一场景</button><button id="export">导出评分 JSON</button></header>
<main id="view"></main>
<script>
const cases=__DATA__, grades=__GRADES__, headings=__HEADINGS__; let index=0; const scores={};
const storeKey='__SCORE_FILENAME__';try{Object.assign(scores,JSON.parse(localStorage.getItem(storeKey)||'{}'))}catch{}
const save=()=>{try{localStorage.setItem(storeKey,JSON.stringify(scores))}catch{}};
const el=(tag,text,cls)=>{const x=document.createElement(tag);if(text!==undefined)x.textContent=String(text);if(cls)x.className=cls;return x};
const add=(parent,tag,text,cls)=>{const x=el(tag,text,cls);parent.append(x);return x};
function render(){const c=cases[index],root=document.getElementById('view');root.replaceChildren();
 document.getElementById('progress').textContent=`${index+1}/${cases.length} · ${c.scenario_id} · ${c.split} · ${c.category}`;
 c.turns.forEach(t=>{add(root,'h2',`第 ${t.turn_index} 轮：${t.user_query}`);const grid=add(root,'div',undefined,'grid');
 Object.keys(headings).forEach(label=>{const a=t.arms[label],box=add(grid,'section',undefined,'arm');add(box,'h3',headings[label]);
 add(box,'p',`状态：${a.display_status}${a.published?'':'（未发布正文）'}`,'muted');add(box,'pre',a.answer||'〔用户未收到正文〕');
 if(a.admin_draft){const detail=add(box,'details');add(detail,'summary','管理员草稿（用户未看到）');add(detail,'pre',a.admin_draft)}
 add(box,'h4',`正文引用 ${a.citations.length} 项`);a.citations.forEach(ref=>{const card=add(box,'div',undefined,'source');add(card,'strong',`${ref.label} · ${ref.document||'来源未匹配'}`);add(card,'p',`位置：${ref.locator??'未知'}`);add(card,'pre',ref.excerpt||'〔未获得对应片段〕')});
 const key=`${c.scenario_id}:T${t.turn_index}:${label}`;add(box,'label','等级');const select=add(box,'select');
 add(select,'option','请选择').value='';grades.forEach(g=>add(select,'option',g).value=g);select.value=scores[key]?.grade||'';
 select.onchange=()=>{scores[key]={...scores[key],grade:select.value};save()};add(box,'label','评审备注');const note=add(box,'textarea');
 note.value=scores[key]?.note||'';note.oninput=()=>{scores[key]={...scores[key],note:note.value};save()};
 });const gold=add(root,'section',undefined,'gold');add(gold,'h3','原文标准与边界');add(gold,'p',`可答性：${t.answerability}`);
 add(gold,'h4','必要要点');t.required_points.forEach(x=>add(gold,'p',`• ${x}`));
 if(t.prohibited_inferences.length){add(gold,'h4','禁止推断');t.prohibited_inferences.forEach(x=>add(gold,'p',`• ${x}`))}
 add(gold,'h4','原文摘录');t.evidence.forEach(x=>{const card=add(gold,'div',undefined,'source');add(card,'strong',`${x.document_id} · ${x.locator} · SHA256 ${x.excerpt_sha256.slice(0,12)}`);add(card,'pre',x.text)})});}
document.getElementById('prev').onclick=()=>{if(index>0){index--;render()}};
document.getElementById('next').onclick=()=>{if(index<cases.length-1){index++;render()}};
document.getElementById('export').onclick=()=>{const data={schema_version:1,case_sha256:'__CASE_SHA__',blind_seed:__BLIND_SEED__,scores};
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
 a.download='__SCORE_FILENAME__';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),30000)};render();
</script></body></html>"""
    return (
        template.replace("__DATA__", payload)
        .replace("__GRADES__", grades)
        .replace("__HEADINGS__", heading_data)
        .replace("__TITLE__", html.escape(title))
        .replace("__SCORE_FILENAME__", score_filename)
    )


def main() -> None:
    """生成匿名和具名评审页、受限诊断与单独的揭盲键。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, action="append", required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    args = parser.parse_args()
    case_raw = args.cases.read_bytes()
    cases = [json.loads(line) for line in case_raw.splitlines() if line.strip()]
    sources = _source_lookup(args.sources)
    args.outdir.mkdir(mode=0o700, exist_ok=False)
    generator = random.Random(BLIND_SEED)  # noqa: S311 - 仅随机化展示标签。
    mapping = {}
    review = []
    diagnostics = []
    for case in cases:
        case_id = case["scenario_id"]
        labels = ["A", "B"] if generator.getrandbits(1) else ["B", "A"]
        mapping[case_id] = {"X": labels[0], "Y": labels[1]}
        turns = []
        for turn in case["turns"]:
            sides = {}
            for label, side in mapping[case_id].items():
                visible, diagnostic = _turn_result(
                    args.run_dir, case_id, turn["turn_index"], side
                )
                sides[label] = visible
                diagnostics.append(
                    {
                        "scenario_id": case_id,
                        "turn_index": turn["turn_index"],
                        "side": side,
                        **diagnostic,
                    }
                )
            evidence = []
            for item in turn["evidence"]:
                source = sources[item["restricted_excerpt_ref"]]
                if source["excerpt_sha256"] != item["excerpt_sha256"]:
                    raise ValueError(f"证据哈希不一致：{case_id}")
                evidence.append(source)
            turns.append(
                {
                    "turn_index": turn["turn_index"],
                    "user_query": turn["user_query"],
                    "answerability": turn["answerability"],
                    "required_points": turn["required_points"],
                    "prohibited_inferences": turn["prohibited_inferences"],
                    "evidence": evidence,
                    "arms": sides,
                }
            )
        review.append(
            {
                "scenario_id": case_id,
                "split": case["split"],
                "category": case["category"],
                "turns": turns,
            }
        )
    case_sha = hashlib.sha256(case_raw).hexdigest()
    html_text = (
        _render_html(
            review,
            title="湾事通隔离 A/B 匿名评审",
            headings={"X": "方案 X", "Y": "方案 Y"},
            score_filename="wkab-blind-scores.json",
        )
        .replace("__CASE_SHA__", case_sha)
        .replace("__BLIND_SEED__", str(BLIND_SEED))
    )
    _write_new(args.outdir / "blind_review.html", html_text.encode())
    diagnostic_index = {
        (item["scenario_id"], item["turn_index"], item["side"]): item
        for item in diagnostics
    }
    named_review = []
    for case in review:
        named_turns = []
        for turn in case["turns"]:
            named_arms = {}
            for label, side in mapping[case["scenario_id"]].items():
                arm = dict(turn["arms"][label])
                diagnosis = diagnostic_index[
                    (case["scenario_id"], turn["turn_index"], side)
                ]
                if side == "A" and not arm["published"]:
                    arm["admin_draft"] = diagnosis.get("draft")
                named_arms[side] = arm
            named_turns.append({**turn, "arms": named_arms})
        named_review.append({**case, "turns": named_turns})
    named_html = (
        _render_html(
            named_review,
            title="湾事通隔离 A/B 具名对照",
            headings={
                "A": "A · 现有自研 Q1（dca24a8）",
                "B": "B · 腾讯原版 WeKnora v0.8.2",
            },
            score_filename="wkab-labeled-scores.json",
        )
        .replace("__CASE_SHA__", case_sha)
        .replace("__BLIND_SEED__", str(BLIND_SEED))
    )
    _write_new(args.outdir / "labeled_review.html", named_html.encode())
    _write_new(
        args.outdir / "labeled_results.jsonl",
        (
            "\n".join(
                json.dumps(case, ensure_ascii=False) for case in named_review
            )
            + "\n"
        ).encode(),
    )
    _write_new(
        args.outdir / "blind_key.private.json",
        json.dumps(mapping, ensure_ascii=False, indent=2).encode(),
    )
    _write_new(
        args.outdir / "diagnostics.private.jsonl",
        (
            "\n".join(json.dumps(x, ensure_ascii=False) for x in diagnostics)
            + "\n"
        ).encode(),
    )
    print(
        json.dumps(
            {
                "scenarios": len(review),
                "turns": sum(len(case["turns"]) for case in review),
                "case_sha256": case_sha,
                "html_bytes": (args.outdir / "blind_review.html")
                .stat()
                .st_size,
            }
        )
    )


if __name__ == "__main__":
    main()
