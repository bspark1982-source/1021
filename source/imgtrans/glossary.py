"""용어집: 영어 용어 → 고정 한글 표기 (CSV: en,ko / UTF-8)."""
from __future__ import annotations

import csv
import os
import re
from typing import Dict, List, Tuple


KEEP = "="   # 한글 칸에 '=' → 번역하지 않음(보호)


class Glossary:
    def __init__(self, path: str):
        self.path = path
        self.items: List[Tuple[str, str]] = []
        self.load()

    # ---------- 파일 입출력 ----------
    def load(self):
        self.items = []
        if not os.path.exists(self.path):
            return
        for enc in ("utf-8-sig", "cp949"):
            try:
                with open(self.path, newline="", encoding=enc) as f:
                    rows = list(csv.reader(f))
                break
            except UnicodeDecodeError:
                continue
        else:
            return
        for r in rows:
            if len(r) < 2:
                continue
            en, ko = r[0].strip(), r[1].strip()
            if not en or en.startswith("#") or en.lower() in ("en", "english", "영어"):
                continue
            self.items.append((en, ko))

    def save(self):
        with open(self.path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["en", "ko"])
            for en, ko in self.items:
                w.writerow([en, ko])

    # ---------- 적용 ----------
    def _sorted(self):
        return sorted([i for i in self.items if i[1] != KEEP], key=lambda x: -len(x[0]))

    def is_protected(self, text: str) -> bool:
        """한글 칸이 '=' 인 항목(로고·약어 등)은 번역하지 않고 원본 그대로 둠."""
        return self.exact(text) == KEEP

    def exact(self, text: str):
        """원문 전체가 용어집 항목과 일치하면 바로 한글 반환(지도 라벨·범례 등)."""
        # 대소문자까지 같은 항목 우선(로고 'INSTITUTE FOR THE STUDY OF WAR' = 보호,
        # 문장 속 'Institute for the Study of War' = 전쟁연구소 처럼 구분 가능)
        raw = _norm(text, lower=False)
        for en, ko in self.items:
            if _norm(en, lower=False) == raw:
                return ko
        key = raw.lower()
        for en, ko in self.items:
            if _norm(en) == key:
                return ko
        return None

    def presubstitute(self, text: str) -> str:
        """번역기에 보내기 전에 원문 속 용어를 한글로 치환해 표기를 고정."""
        out = text
        for en, ko in self._sorted():
            pat = r"(?<![A-Za-z])" + re.escape(en) + r"(?![A-Za-z])"
            out = re.sub(pat, ko, out, flags=re.IGNORECASE)
        return out

    def as_dict(self) -> Dict[str, str]:
        return dict(self.items)


def _norm(s: str, lower: bool = True) -> str:
    s = re.sub(r"[\s\.:;,]+", " ", s.strip()).strip()
    return s.lower() if lower else s
