"""Discover notices before planning; only explicitly reviewed terms enter accounts.
Pagination and the independent dividend/split table check follow R5 workflow_discovery.
"""

import argparse
from datetime import date, datetime, time
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .config import CODES, digest
from .events import CN, stamp
from .gate.order_loop import exclusive_run

ACTION = re.compile(
    "分红|红利发放|权益分派|收益分配|除息|除权|拆分|分拆|折算|缩份|份额合并|份额转换|更正|修订.*分配"
)
EMPTY_TABLE = re.compile("暂无|尚无|没有.*(分红|拆分)|未进行.*(分红|拆分)")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
        temporary = f.name
    os.replace(temporary, path)


class Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self.table = self.row = self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []

    def handle_data(self, text):
        if self.cell is not None:
            self.cell.append(text)

    def handle_endtag(self, tag):
        if tag in {"th", "td"} and self.cell is not None:
            self.row.append("".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def fetch_public(url):
    host = urlsplit(url).hostname
    if host not in ("api.fund.eastmoney.com", "fundf10.eastmoney.com"):
        raise ValueError("Unexpected notice source")
    request = Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fundf10.eastmoney.com/"}
    )
    with urlopen(request, timeout=25) as response:
        data = response.read(16 * 1024 * 1024 + 1)
    if not 0 < len(data) <= 16 * 1024 * 1024:
        raise ValueError("Invalid notice response size")
    return data


def economic_tables(raw):
    parser = Tables()
    parser.feed(raw.decode("utf-8-sig"))
    result = []
    for label in ("权益登记日", "拆分折算日"):
        tables = [t for t in parser.tables if t and label in t[0]]
        if len(tables) != 1:
            raise ValueError("Incomplete dividend/split table")
        result.append(tables[0])
    return result


def fund_index(code, fetch):
    items = {}
    total = None
    count = 0
    for page in range(1, 1001):
        url = f"https://api.fund.eastmoney.com/f10/JJGG?fundcode={code[:6]}&pageIndex={page}&pageSize=100&type=0"
        data = json.loads(fetch(url))
        rows = data["Data"]
        if (
            not isinstance(rows, list)
            or data["PageIndex"] != page
            or data.get("ErrCode") not in (0, None)
            or type(data["TotalCount"]) is not int
            or data["TotalCount"] < 0
        ):
            raise ValueError(f"{code}: invalid pagination")
        if total is None:
            total = data["TotalCount"]
            if total == 0:
                raise ValueError(f"{code}: missing announcement history for listed baseline ETF")
        if total != data["TotalCount"]:
            raise ValueError(f"{code}: index changed during scan")
        for row in rows:
            nid = f"{code}/notice/{row['ID']}"
            if not row["ID"] or row["FUNDCODE"] != code[:6] or nid in items:
                raise ValueError(f"{code}: bad or repeated notice")
            title = str(row.get("TITLE") or row.get("NOTICETITLE") or "").strip()
            if not title:
                raise ValueError(f"{code}: missing notice title")
            published = str(date.fromisoformat(row["PUBLISHDATE"][:10]))
            items[nid] = dict(
                code=code,
                date=published,
                title=title,
                kind="notice",
                source=url,
                revision=digest(row),
                relevant=bool(ACTION.search(title)),
            )
        count += len(rows)
        if count == total:
            break
        if not rows or count > total:
            raise ValueError(f"{code}: incomplete index")
    else:
        raise ValueError(f"{code}: page limit exceeded")
    url = f"https://fundf10.eastmoney.com/fhsp_{code[:6]}.html"
    tables = economic_tables(fetch(url))
    if tables != economic_tables(fetch(url)):
        raise ValueError(f"{code}: inconsistent independent event table")
    for kind, table in zip(("dividend", "split"), tables):
        for row in table[1:]:
            if len(row) == 1 and EMPTY_TABLE.search(row[0]):
                continue
            if len(row) != len(table[0]):
                raise ValueError(f"{code}: unknown event table row")
            fields = dict(zip(table[0], row))
            field = "权益登记日" if kind == "dividend" else "拆分折算日"
            event_day = str(date.fromisoformat(fields[field]))
            nid = f"{code}/{kind}/{event_day}"
            if nid in items:
                raise ValueError(f"{code}: ambiguous action")
            items[nid] = dict(
                code=code,
                date=event_day,
                kind=kind,
                fields=fields,
                revision=digest(fields),
                source=url,
                relevant=True,
            )
    return items


def scan(path, through, baseline, fetch=fetch_public, now=None):
    """Replace coverage only after all ten indexes succeed; keep decisions bound to revisions."""
    path = Path(path)
    when = now or datetime.now(CN)
    cutoff = datetime.combine(date.fromisoformat(through), time(17), CN)
    if when < cutoff or date.fromisoformat(baseline) > cutoff.date():
        raise ValueError("Invalid discovery cutoff")
    with exclusive_run(path.parent):
        old = read(path) if path.exists() else {}
        if old and (baseline != old["baseline"] or through < old.get("through", baseline)):
            raise ValueError("Discovery history cannot be reset")
        state = {**old, "baseline": baseline, "status": "INCOMPLETE", "through": through}
        save(path, state)
        entries = {}
        for code in CODES.values():
            entries.update(fund_index(code, fetch))
        previous = old.get("entries", {})
        pending = dict(old.get("pending", {}))
        changes = []
        decisions = old.get("decisions", {})
        for key in sorted(set(entries) | set(previous)):
            current = entries.get(key)
            before = previous.get(key)
            item = current or before
            changed = before is not None and (not current or before["revision"] != current["revision"])
            fresh = before is None and (item["date"] > baseline or bool(previous))
            if item["kind"] == "notice" and item["date"] > str(when.date()):
                raise ValueError("Future announcement publication date")
            if not (changed or fresh):
                continue
            changes.append(
                dict(
                    id=key,
                    change="removed" if not current else "changed" if changed else "new",
                    title=item.get("title", item["kind"]),
                    date=item["date"],
                )
            )
            if not (item["relevant"] or (before and before["relevant"])):
                continue
            rev = digest([key, current["revision"] if current else "removed"])
            for stale in [r for r, p in pending.items() if p["id"] == key and r != rev]:
                del pending[stale]
            pending[rev] = dict(id=key, change="removed" if not current else "review", item=item)
        state.update(
            entries=entries,
            pending=pending,
            changes=changes,
            decisions=decisions,
            status="COMPLETE",
            observed_at=when.isoformat(),
            codes=list(CODES.values()),
        )
        save(path, state)
        return state


def reviewed_actions(state, through, as_of):
    cutoff = datetime.combine(date.fromisoformat(through), time(17), CN)
    if (
        state.get("status") != "COMPLETE"
        or state.get("through") != through
        or state.get("pending") != {}
        or set(state.get("codes", [])) != set(CODES.values())
        or not cutoff <= stamp(state["observed_at"]) <= as_of
    ):
        raise ValueError("Announcement coverage incomplete, stale or pending review")
    terms = {}
    for decision in state.get("decisions", {}).values():
        if stamp(decision["reviewed_at"]) > as_of:
            raise ValueError("Future notice review")
        event = decision.get("event")
        if event:
            key = (event["key"], event.get("ex_date") or event["effective_date"])
            if key in terms and terms[key] != event:
                raise ValueError("Conflicting approved terms")
            terms[key] = event
    return [terms[key] for key in sorted(terms)]


def terms_identity(state, through, as_of):
    terms = reviewed_actions(state, through, as_of)
    return digest([state["baseline"], through, terms])


def review(path, records, now=None):
    when = now or datetime.now(CN)
    with exclusive_run(Path(path).parent):
        state = read(path)
        if state["status"] != "COMPLETE":
            raise ValueError("Finish the scan before review")
        for record in records:
            rev = record["revision"]
            candidate = state["pending"][rev]
            item = candidate["item"]
            ref = urlsplit(record["official_source"])
            if ref.scheme != "https" or not ref.hostname or not record.get("note"):
                raise ValueError("Document the official terms or exclusion reason")
            event = record.get("event")
            if record["decision"] == "accept":
                inv = {v: k for k, v in CODES.items()}
                if not event or event.get("key") != inv[item["code"]]:
                    raise ValueError("Wrong event identity")
                if "ex_date" in event:
                    dates = [
                        date.fromisoformat(event[k]) for k in ("record_date", "ex_date", "payment_date")
                    ]
                    if not dates[0] < dates[1] <= dates[2] or not 0 < float(
                        event["cash_per_actual_share"]
                    ) < float("inf"):
                        raise ValueError("Invalid verified dividend terms")
                else:
                    date.fromisoformat(event["effective_date"])
                    factor = float(event["new_shares_per_old_share"])
                    if not 0 < factor < float("inf") or (
                        not factor.is_integer() and event.get("rounding") != "ceil"
                    ):
                        raise ValueError("Unsupported split terms")
                event = {
                    **event,
                    "event_id": digest([event["key"], event.get("ex_date") or event["effective_date"]]),
                }
            elif record["decision"] != "exclude" or event:
                raise ValueError("Explicit accept/exclude required")
            # Corrections replace the earlier approved terms only on explicit review.
            previous_reviews = {r for r, v in state["decisions"].items() if v["id"] == candidate["id"]}
            for previous in previous_reviews.union(record.get("supersedes", [])):
                prior = state["decisions"][previous]
                if prior["code"] != item["code"]:
                    raise ValueError("Cross-fund revision")
                prior["event"] = None
            state["decisions"][rev] = dict(
                code=item["code"],
                id=candidate["id"],
                event=event,
                reviewed_at=when.isoformat(),
                official_source=ref.geturl(),
                note=record["note"],
            )
            del state["pending"][rev]
        save(path, state)


def main():
    ap = argparse.ArgumentParser(description="V12 公告发现与官方条款复核")
    ap.add_argument("action", choices=["scan", "review"])
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--through")
    ap.add_argument("--baseline", help="首次已核实的历史权益截止日，后续保持相同")
    ap.add_argument("--decisions", type=Path)
    a = ap.parse_args()
    if a.action == "scan":
        if not a.through or not a.baseline:
            ap.error("scan requires --through and --baseline")
        state = scan(a.state, a.through, a.baseline)
        print("已检索", len(state["entries"]), "项，待核实", len(state["pending"]))
    else:
        review(a.state, read(a.decisions))
        print("待核实", len(read(a.state)["pending"]))


if __name__ == "__main__":
    main()
