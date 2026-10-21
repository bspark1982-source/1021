"""무료 번역 엔진 (API 키 불필요). 품질보다 편의 위주 → 결과는 편집 화면에서 검수.

엔진
- google  : 구글 번역 웹 무료 엔드포인트(비공식). 품질 양호, 가끔 차단될 수 있음
- mymemory: MyMemory 무료 번역(하루 글자 수 제한 있음). 구글이 막힐 때 대안
- none    : 자동 번역 안 함(용어집만 적용, 나머지는 수작업/파파고 붙여넣기)
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from typing import Callable, List, Optional

ENGINES = {
    "google": "구글 번역 (무료 웹)",
    "mymemory": "MyMemory (무료, 일일 제한)",
    "none": "자동 번역 안 함 (수작업)",
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def _http_get(url: str, timeout: float = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


# ---------------- Google (gtx) ----------------
def parse_google(payload: str) -> str:
    data = json.loads(payload)
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


def google_translate(text: str, src="en", dst="ko") -> str:
    q = urllib.parse.quote(text)
    url = ("https://translate.googleapis.com/translate_a/single"
           f"?client=gtx&sl={src}&tl={dst}&dt=t&q={q}")
    return parse_google(_http_get(url))


# ---------------- MyMemory ----------------
def mymemory_translate(text: str, src="en", dst="ko") -> str:
    q = urllib.parse.quote(text[:480])
    url = f"https://api.mymemory.translated.net/get?q={q}&langpair={src}|{dst}"
    data = json.loads(_http_get(url))
    return data.get("responseData", {}).get("translatedText", "") or ""


def _one(engine: str, text: str) -> str:
    if engine == "google":
        return google_translate(text)
    if engine == "mymemory":
        return mymemory_translate(text)
    return ""


def translate_many(texts: List[str], engine: str = "google",
                   progress: Optional[Callable[[int, int], None]] = None,
                   delay: float = 0.15) -> List[Optional[str]]:
    """여러 문단을 번역. 실패한 항목은 None.

    구글 엔진은 여러 문단을 줄바꿈으로 묶어 한 번에 보내고(빠름),
    줄 수가 어긋나면 하나씩 다시 보냄.
    """
    n = len(texts)
    out: List[Optional[str]] = [None] * n
    if engine == "none" or n == 0:
        return out
    clean = [re.sub(r"\s+", " ", t).strip() for t in texts]

    done = 0
    if engine == "google":
        batch, size = [], 0
        batches = []
        for i, t in enumerate(clean):
            if batch and (size + len(t) > 1800 or len(batch) >= 25):
                batches.append(batch)
                batch, size = [], 0
            batch.append(i)
            size += len(t) + 1
        if batch:
            batches.append(batch)
        for bt in batches:
            joined = "\n".join(clean[i] for i in bt)
            ok = False
            try:
                res = google_translate(joined)
                parts = [p.strip() for p in res.split("\n")]
                if len(parts) == len(bt):
                    for i, p in zip(bt, parts):
                        out[i] = p
                    ok = True
            except Exception:
                pass
            if not ok:
                for i in bt:
                    try:
                        out[i] = google_translate(clean[i])
                    except Exception:
                        out[i] = None
                    time.sleep(delay)
            done += len(bt)
            if progress:
                progress(done, n)
            time.sleep(delay)
        return out

    for i, t in enumerate(clean):
        try:
            out[i] = _one(engine, t)
        except Exception:
            out[i] = None
        done += 1
        if progress:
            progress(done, n)
        time.sleep(delay)
    return out


# ---------------- 수작업(파파고·DeepL 웹 붙여넣기) 지원 ----------------
def export_numbered(texts: List[str]) -> str:
    lines = []
    for i, t in enumerate(texts):
        one = re.sub(r"\s+", " ", t).strip()
        lines.append("[%d] %s" % (i + 1, one))
    return "\n".join(lines)


def import_numbered(text: str, n: int) -> List[Optional[str]]:
    """'[번호] 번역문' 형식을 다시 읽어옴. 번역기가 괄호를 바꿔도 최대한 인식."""
    out: List[Optional[str]] = [None] * n
    pat = re.compile(r"^\s*[\[［【(（]\s*(\d+)\s*[\]］】)）]\s*(.*)$")
    cur = None
    for line in text.splitlines():
        m = pat.match(line)
        if m:
            cur = int(m.group(1)) - 1
            if 0 <= cur < n:
                out[cur] = m.group(2).strip()
            else:
                cur = None
        elif cur is not None and line.strip():
            out[cur] = ((out[cur] or "") + " " + line.strip()).strip()
    return out
