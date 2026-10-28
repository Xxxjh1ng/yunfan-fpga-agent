"""智能体 4：调试修复（Log-Guided Minimal Repair）。

修复顺序遵循「代价从低到高」：
1. **确定性规则**（毫秒级、零 token）：5~6 类错误可直接机械修复；
2. **最小编辑补丁**（LLM 只输出 find/replace 对，不重写全文）：降低截断风险、
   保留已被验证正确的代码；
3. **整模块重写**（兜底）：仅在补丁无法表达时使用。

每一步都会记录方法、触发类别与 token 开销，构成论文中「修复策略有效性」的数据来源。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..diagnostics.parser import Diagnostic
from ..llm import prompts
from ..orchestration.spec import DesignSpec
from ..rtl.extract import inspect_structure, mask_noncode
from ..rtl.interface import compare_ports, parse_interface
from ..rtl.sanitize import ensure_endmodule, fix_wire_assigned_in_always, strip_display_in_dut, strip_hash_delays, sanitize
from ..eda.constraints import remap_invalid_pins
from .base import BaseAgent, extract_json


@dataclass
class RepairResult:
    code: str = ""
    cst: str = ""
    method: str = "none"  # deterministic / llm-patch / llm-rewrite / none
    applied: List[str] = field(default_factory=list)
    diagnosis: str = ""
    tokens: int = 0
    changed: bool = False

    def brief(self) -> str:
        return f"{self.method}：{'; '.join(self.applied) if self.applied else '无变更'}"


# --------------------------------------------------------------------------
# 确定性修复
# --------------------------------------------------------------------------
def close_end_before_else(code: str) -> Tuple[str, int]:
    """修复「if(...) begin … else」中间丢失 end 的常见残缺。

    判据：遇到 else 时向上回溯到同层的 if，若该 if 以 begin 开块，且 else 前
    最后一行不是 end，则说明块未闭合，插入一个 end。
    """
    lines = code.split("\n")
    out: List[str] = []
    inserted = 0
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("else"):
            indent = len(line) - len(line.lstrip())
            j = len(out) - 1
            while j >= 0 and not out[j].strip():
                j -= 1
            prev = out[j].strip() if j >= 0 else ""
            needs_end = prev != "end"
            if needs_end:
                # 向上找同层 if
                k = j
                while k >= 0:
                    s = out[k].strip()
                    k_indent = len(out[k]) - len(out[k].lstrip())
                    if s.startswith("if") and k_indent <= indent:
                        if "begin" in s:
                            out.append(" " * indent + "end")
                            inserted += 1
                        break
                    k -= 1
        out.append(line)
    return "\n".join(out), inserted


def close_end_before_case_item(code: str) -> Tuple[str, int]:
    """修复「case 分支内部 begin 未闭合就进入下一个分支」的残缺。

    判据：case 内的分支标签处，begin/end 深度必须与标签层级一致；
    若深度过深，说明上一分支缺少 end，就地补齐。
    """
    masked_lines = mask_noncode(code).split("\n")
    lines = code.split("\n")
    out: List[str] = []
    inserted = 0
    depth = 0
    case_stack: List[int] = []
    for idx, line in enumerate(lines):
        s = masked_lines[idx].strip()
        if re.match(r"^case[xz]?\b", s):
            case_stack.append(depth)
        elif s.startswith("endcase"):
            if case_stack:
                case_stack.pop()
        else:
            m = re.match(r"^(default|[A-Za-z0-9_'\[\]\. ]+?)\s*:\s*(begin)?\s*$", s)
            if case_stack and m:
                # case 分支内部的 begin 必须全部闭合，深度应回到 case 语句所在层
                want = case_stack[-1]
                if depth > want:
                    n = depth - want
                    out.extend([" " * (len(line) - len(line.lstrip())) + "end"] * n)
                    inserted += n
                    depth = want
        out.append(line)
        depth += len(re.findall(r"\bbegin\b", masked_lines[idx])) - len(re.findall(r"\bend\b", masked_lines[idx]))
    return "\n".join(out), inserted


def close_missing_end(code: str) -> Tuple[str, List[str]]:
    """补齐 begin/end 与 endmodule。"""
    notes: List[str] = []
    code, n_else = close_end_before_else(code)
    if n_else:
        notes.append(f"在 {n_else} 处 else 前补齐 end")
    code, n_case = close_end_before_case_item(code)
    if n_case:
        notes.append(f"在 {n_case} 处 case 分支前补齐 end")
    masked = mask_noncode(code)
    opens = len(re.findall(r"\bbegin\b", masked))
    ends = len(re.findall(r"\bend\b", masked))
    if opens > ends:
        need = opens - ends
        idx = masked.rfind("endmodule")
        insert_at = idx if idx > 0 else len(code)
        code = code[:insert_at] + ("end\n" * need) + code[insert_at:]
        notes.append(f"补齐 {need} 个 end")
    code, changed = ensure_endmodule(code)
    if changed:
        notes.append("补齐 endmodule")
    return code, notes


def _split_port_segments(inner: str) -> List[str]:
    segs, depth, cur = [], 0, []
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        segs.append("".join(cur))
    return segs


def _header_bounds(code: str) -> Optional[Tuple[int, int, int]]:
    """返回 (模块名起始, 端口列表 '(' 下标, 配对 ')' 下标)。"""
    masked = mask_noncode(code)
    m = re.search(r"\bmodule\s+([A-Za-z_]\w*)", masked)
    if not m:
        return None
    i = m.end()
    n = len(masked)
    while i < n and masked[i] in " \t\r\n":
        i += 1
    if i < n and masked[i] == "#":
        # 跳过参数列表
        j = masked.find("(", i)
        if j < 0:
            return None
        depth = 0
        while j < n:
            if masked[j] == "(":
                depth += 1
            elif masked[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        i = j + 1
    while i < n and masked[i] in " \t\r\n":
        i += 1
    if i >= n or masked[i] != "(":
        return None
    start = i
    depth = 0
    while i < n:
        if masked[i] == "(":
            depth += 1
        elif masked[i] == ")":
            depth -= 1
            if depth == 0:
                return m.start(1), start, i
        i += 1
    return None


def rewrite_ansi_header(code: str, spec_ports: Sequence[dict]) -> Tuple[str, List[str]]:
    """按规格重写 ANSI 端口表：补缺失端口、修正方向/位宽、删除多余端口。"""
    bounds = _header_bounds(code)
    if not bounds:
        return code, []
    _, lparen, rparen = bounds
    inner = code[lparen + 1 : rparen]
    if not re.search(r"\b(input|output|inout)\b", inner, re.IGNORECASE):
        return code, []

    parsed: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for seg in _split_port_segments(inner):
        dm = re.match(r"\s*(input|output|inout)\b(.*)$", seg, re.IGNORECASE | re.DOTALL)
        if not dm:
            continue
        direction = dm.group(1).lower()
        rest = dm.group(2)
        wm = re.search(r"\[[^\]]*\]", rest)
        width_expr = wm.group(0) if wm else ""
        rest_wo = rest.replace(width_expr, " ") if width_expr else rest
        for raw_name in rest_wo.split(","):
            name = re.sub(r"\[[^\]]*\]", "", raw_name).strip()
            name = name.split()[-1] if name.split() else ""
            if re.fullmatch(r"[A-Za-z_]\w*", name or ""):
                kind = "reg" if re.search(r"\breg\b", rest, re.IGNORECASE) else "wire"
                parsed[name.lower()] = {"name": name, "dir": direction, "width_expr": width_expr, "kind": kind}
                order.append(name.lower())

    notes: List[str] = []
    spec_names = {str(p.get("name")).lower() for p in spec_ports}
    lines: List[str] = []
    for p in spec_ports:
        name = str(p.get("name"))
        width = int(p.get("width") or 1)
        direction = str(p.get("dir") or "input").lower()
        want_expr = "" if width <= 1 else f"[{width - 1}:0]"
        cur = parsed.get(name.lower())
        if cur is None:
            lines.append(f"    {direction} wire {want_expr + ' ' if want_expr else ''}{name}")
            notes.append(f"补端口 {name}（{direction}{width}）")
        else:
            # 保留原有的 wire/reg 限定符（输出若原是 reg，不能被改写成 wire）
            kind = cur.get("kind", "wire")
            seg = f"    {direction} {kind} {want_expr + ' ' if want_expr else ''}{name}"
            if cur["dir"] != direction:
                notes.append(f"修正 {name} 方向 {cur['dir']}->{direction}")
            if (cur["width_expr"] or "").replace(" ", "") != want_expr.replace(" ", ""):
                notes.append(f"修正 {name} 位宽 {cur['width_expr'] or '1'}->{want_expr or '1'}")
            lines.append(seg)

    removed = [n for n in order if n not in spec_names]
    if removed:
        notes.append("删除多余端口 " + ", ".join(removed))

    if not notes:
        return code, []
    new_header = "(\n" + ",\n".join(lines) + "\n)"
    return code[:lparen] + new_header + code[rparen + 1 :], notes


class DebugAgent(BaseAgent):
    name = "调试修复"
    role = "依据结构化诊断做最小编辑修复"

    # ------------------------------------------------------------------
    def repair(
        self,
        spec: DesignSpec,
        code: str,
        diagnostics: Sequence[Diagnostic],
        *,
        cst: str = "",
        log_tail: str = "",
        attempt: int = 1,
        device=None,
        gowin_root: Optional[str] = None,
    ) -> RepairResult:
        taxa = {d.taxon for d in diagnostics}
        applied: List[str] = []
        new_code = code
        new_cst = cst

        # ---------- 1. 确定性修复 ----------
        if "E10" in taxa and cst:
            new_cst, changes = remap_invalid_pins(cst, device, gowin_root)
            if changes:
                applied.extend(changes)
        if "E02" in taxa or "E01" in taxa:
            new_code, notes = close_missing_end(new_code)
            applied.extend(notes)
        if "E04" in taxa:
            new_code, changed = fix_wire_assigned_in_always(new_code)
            if changed:
                applied.append("wire -> reg")
        if "E08" in taxa:
            new_code, ch1 = strip_hash_delays(new_code)
            new_code, ch2 = strip_display_in_dut(new_code)
            if ch1:
                applied.append("删除 # 延时")
            if ch2:
                applied.append("删除系统任务")
        if "E09" in taxa:
            new_code, notes = rewrite_ansi_header(new_code, spec.ports)
            applied.extend(notes)

        if applied:
            self.log("确定性修复", "；".join(applied[:4]))
            return RepairResult(
                code=new_code,
                cst=new_cst,
                method="deterministic",
                applied=applied,
                diagnosis="由确定性规则库直接修复：" + ", ".join(sorted(taxa)),
                changed=(new_code != code or new_cst != cst),
            )

        # ---------- 2. LLM 最小编辑补丁 ----------
        diag_json = json.dumps([d.as_dict() for d in diagnostics[:12]], ensure_ascii=False, indent=2)
        user = prompts.debug_user(spec.to_json(), code, diag_json, log_tail[-4000:], attempt)
        resp = self.ctx.llm(self.name, prompts.DEBUG_SYSTEM, user, tag=f"debug-{attempt}", json_mode=True)
        try:
            patch = extract_json(resp.text)
        except Exception as exc:  # noqa: BLE001
            self.warn("补丁解析失败", str(exc)[:160])
            return RepairResult(code=code, cst=cst, method="none", tokens=resp.total_tokens)

        diagnosis = str(patch.get("diagnosis") or "")
        rewrite = patch.get("rewrite")
        if isinstance(rewrite, str) and rewrite.strip():
            san = sanitize(rewrite, target_name=spec.top_module)
            self.log("整模块重写", diagnosis[:80], tokens=resp.total_tokens)
            return RepairResult(
                code=san.code, cst=new_cst, method="llm-rewrite",
                applied=["模型重写完整模块"], diagnosis=diagnosis,
                tokens=resp.total_tokens, changed=san.code != code,
            )

        patches = patch.get("fixes") or []
        out = code
        hits = 0
        for item in patches:
            if not isinstance(item, dict):
                continue
            find = str(item.get("find") or "")
            repl = str(item.get("replace") or "")
            if not find:
                continue
            if find in out:
                out = out.replace(find, repl, 1)
                hits += 1
                applied.append(f"替换 `{find.strip().splitlines()[0][:40]}`")
                continue
            # 宽松匹配：允许行首缩进差异
            stripped = find.strip()
            pattern = re.escape(stripped).replace(r"\ ", r"\s+")
            m = re.search(pattern, out)
            if m:
                out = out[: m.start()] + repl + out[m.end():]
                hits += 1
                applied.append(f"宽松替换 `{stripped.splitlines()[0][:40]}`")
        if hits == 0:
            self.warn("补丁未命中任何片段", "退回原始代码")
            return RepairResult(code=code, cst=cst, method="none", diagnosis=diagnosis, tokens=resp.total_tokens)

        san = sanitize(out, target_name=spec.top_module)
        self.log("最小编辑补丁", f"{hits} 处；{diagnosis[:60]}", tokens=resp.total_tokens)
        return RepairResult(
            code=san.code, cst=new_cst, method="llm-patch", applied=applied,
            diagnosis=diagnosis, tokens=resp.total_tokens, changed=san.code != code,
        )

    # ------------------------------------------------------------------
    def repair_raw(self, spec: DesignSpec, code: str, raw_log: str, *, attempt: int = 1) -> RepairResult:
        """对照路径：不做结构化诊断，直接把整段原始日志丢给模型要求重写。

        这正是原始单点方案的做法（simple_agent/readlog.py 的 debug()），
        保留它才能在论文里做同条件消融。
        """
        system = (
            "你是硬件代码修复专家。请根据工具日志修复 Verilog 代码。\n"
            "要求：只输出一个完整模块的 Verilog-2005 代码，从 module 到 endmodule，"
            "不要 Markdown 围栏，不要解释文字。端口名与模块名必须保持不变。"
        )
        user = (
            f"【设计规格】\n{spec.to_json()}\n\n"
            f"【当前 RTL】\n{code}\n\n"
            f"【工具原始日志】\n{raw_log[-12000:]}\n\n请输出修复后的完整 Verilog 代码。"
        )
        resp = self.ctx.llm(self.name, system, user, tag=f"debug-raw-{attempt}")
        san = sanitize(resp.text, target_name=spec.top_module)
        self.log("整模块重写（无结构化诊断）", f"{len(san.code.splitlines())} 行", tokens=resp.total_tokens)
        return RepairResult(
            code=san.code,
            method="llm-rewrite",
            applied=["原始日志 -> 整模块重写"],
            diagnosis="baseline: raw-log rewrite",
            tokens=resp.total_tokens,
            changed=san.code != code,
        )
