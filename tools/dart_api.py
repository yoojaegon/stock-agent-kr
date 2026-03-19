import os
import io
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict
from datetime import date
import re
from html import unescape

import httpx
from dotenv import load_dotenv
load_dotenv()


class DartApiError(Exception):
    """DART API가 HTTP 200이지만 에러 상태를 반환했을 때 발생합니다."""
    def __init__(self, status: str, message: str):
        super().__init__(f"DART API 오류 [{status}]: {message}")
        self.status = status


def _check_dart_response(data: dict) -> None:
    """DART API 응답의 status를 확인하고 에러면 예외를 발생시킵니다."""
    status = data.get("status", "")
    if status not in ("000", "013"):  # 013: 조회 결과 없음 (정상)
        raise DartApiError(status, data.get("message", "알 수 없는 오류"))

DART_API_KEY = os.getenv("DART_API_KEY")
CACHE_DIR = Path(".cache")
CORP_CODE_CACHE = CACHE_DIR / "corp_codes.xml"
CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
_REPORT_CODES = [
    # 보고서 우선순위: 사업보고서 → 3분기 → 반기 → 1분기
    ("11011", "사업보고서"),
    ("11014", "3분기보고서"),
    ("11012", "반기보고서"),
    ("11013", "1분기보고서"),
]
_CHUNK_SIZE = 30_000
_BUSINESS_SECTION = "사업의 내용"
_B_TYPE_MAX_CHARS = 4_000

def _download_corp_codes() -> str:
    # DART에서 전체 기업 코드를 XML로 변환.
    response = httpx.get(CORP_CODE_URL, params={"crtfc_key": DART_API_KEY}, timeout=30)
    response.raise_for_status()
    
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        xml_filename = next(name for name in z.namelist() if name.upper().endswith(".xml"))
        return z.read(xml_filename).decode("utf-8")
    
def get_corp_codes(force_refresh: bool = False) -> ET.Element:
    # 캐시가 없을때만 다운로드함.
    
    if not force_refresh and CORP_CODE_CACHE.exists():
        xml_text = CORP_CODE_CACHE.read_text(encoding="utf-8")
    else:
        xml_text = _download_corp_codes()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CORP_CODE_CACHE.write_text(xml_text, encoding="utf-8")
    
    return ET.fromstring(xml_text)

def find_corp_code(query: str) -> str | None:
    # DART 기업코드(8자리) 반환함.
    # 우선순위: 종목코드 정확 일치 → 기업명 정확 일치 → 기업명 부분 일치
    root = get_corp_codes()
    items = root.findall("list")

    # 1순위: 종목코드 정확 일치
    for item in items:
        if item.findtext("stock_code", "").strip() == query:
            return item.findtext("corp_code")

    # 2순위: 기업명 정확 일치
    for item in items:
        if item.findtext("corp_name", "") == query:
            return item.findtext("corp_code")

    # 3순위: 기업명 부분 일치
    for item in items:
        if query in item.findtext("corp_name", ""):
            return item.findtext("corp_code")
    
    return None

def get_financial_data(corp_code: str, bsns_year: str | None = None) -> Dict | None:
    # 재무 수치를 반환
    
    if bsns_year is None:
        today = date.today()
        bsns_year = str(today.year - 1) if today.month <= 3 else str(today.year)
        
    for reprt_code, report_nm in _REPORT_CODES:
        response = httpx.get(
            "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json",
            params={
                "crtfc_key": DART_API_KEY,
                "corp_code": corp_code,
                "bsns_year": bsns_year,
                "reprt_code": reprt_code,
            },
            timeout=15,
        )
        
        response.raise_for_status()
        data = response.json()
        
        try:
            _check_dart_response(data)
        except DartApiError:
            continue

        if not data.get("list"):
            continue

        items = data["list"]
        
        for fs_div in ("CFS", "OFS"):
            filtered = [x for x in items if x.get("fs_div") == fs_div]
            if filtered:
                return {
                    "report_nm": report_nm,
                    "fs_div": fs_div,
                    "list": filtered,
                }

    return None

def get_disclosure_list(
    corp_code: str,
    bgn_de: str,
    end_de: str,
    pblntf_ty: str | None = None,  # "A": 정기공시, "B": 주요사항보고서, None: 전체
    page_count: int = 20,
) -> list[dict]:
    """
    공시 목록을 반환합니다. rcept_no(접수번호)가 이후 원문 조회에 사용됩니다.

    Args:
        corp_code: DART 기업코드 (8자리)
        bgn_de: 시작일 (YYYYMMDD)
        end_de: 종료일 (YYYYMMDD)
        pblntf_ty: 공시 유형. "A"=정기공시, "B"=주요사항보고서, None=전체
        page_count: 최대 반환 건수 (최대 100)
    """
    params = {
        "crtfc_key": DART_API_KEY,
        "corp_code": corp_code,
        "bgn_de": bgn_de,
        "end_de": end_de,
        "page_count": page_count,
    }
    if pblntf_ty:
        params["pblntf_ty"] = pblntf_ty

    response = httpx.get(
        "https://opendart.fss.or.kr/api/list.json",
        params=params,
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()

    _check_dart_response(data)

    return [
        {
            "rcept_no": item["rcept_no"],
            "report_nm": item["report_nm"],
            "rcept_dt": item["rcept_dt"],
            "corp_name": item["corp_name"],
            "flr_nm": item["flr_nm"],
        }
        for item in data.get("list", [])
    ]


def get_dart_url(rcept_no: str) -> str:
    """rcept_no로 DART 웹 뷰어 URL을 반환합니다."""
    return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


def _fetch_document_zip(rcept_no: str) -> zipfile.ZipFile:
    # DART api 에서 zip을 받아옴
    
    response = httpx.get(
        "https://opendart.fss.or.kr/api/document.xml",
        params={"crtfc_key": DART_API_KEY, "rcept_no": rcept_no},
        timeout=30,
    )
    
    response.raise_for_status()
    return zipfile.ZipFile(io.BytesIO(response.content))

def _largest_html_in_zip(z: zipfile.ZipFile) -> str:
    """ZIP 내 가장 큰 HTML 파일명 반환 (B 유형 — 본문 파일 선택용)."""
    html_files = [
        name for name in z.namelist()
        if name.lower().endswith((".html", ".htm"))
    ]
    return max(html_files, key=lambda name: z.getinfo(name).file_size)


def _table_to_markdown(match: re.Match) -> str:
    """<table>...</table> 하나를 마크다운 테이블로 변환합니다."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", match.group(0), re.DOTALL | re.IGNORECASE)
    md_rows = []
    for i, row in enumerate(rows):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL | re.IGNORECASE)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        cells = [unescape(c) for c in cells]
        md_rows.append("| " + " | ".join(cells) + " |")
        if i == 0:
            md_rows.append("|" + "---|" * len(cells))
    return "\n".join(md_rows)


def _clean_html(html: str) -> str:
    """HTML에서 테이블은 마크다운으로 변환하고 나머지 태그/엔티티를 제거합니다."""
    # <table> → 마크다운
    html = re.sub(
        r"<table[^>]*>.*?</table>",
        _table_to_markdown,
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # 나머지 태그 제거
    html = re.sub(r"<[^>]+>", "", html)
    # HTML 엔티티 디코딩
    html = unescape(html)
    # 공백 정리: 연속 빈줄 2줄로 압축
    html = re.sub(r"\n{3,}", "\n\n", html)
    html = re.sub(r"[ \t]+", " ", html)
    return html.strip()

def _extract_section_html(z: zipfile.ZipFile, section_title: str) -> str | None:
    """
    ZIP에서 섹션 제목에 해당하는 HTML을 반환합니다.
    목차 XML 파싱 실패 시 전체 파일 텍스트 검색으로 fallback합니다.
    """
    # 1. 목차 XML에서 섹션 파일명 찾기
    xml_files = [name for name in z.namelist() if name.lower().endswith(".xml")]
    for xml_file in xml_files:
        try:
            content = z.read(xml_file).decode("utf-8", errors="ignore")
            match = re.search(
                rf"{re.escape(section_title)}.{{0,300}}?([\w\-]+\.html?)",
                content,
                re.DOTALL | re.IGNORECASE,
            )
            if match:
                filename = match.group(1)
                if filename in z.namelist():
                    return z.read(filename).decode("utf-8", errors="ignore")
        except Exception:
            continue

    # 2. fallback: 전체 HTML 파일 중 섹션 제목이 포함된 파일 검색
    html_files = [name for name in z.namelist() if name.lower().endswith((".html", ".htm"))]
    for html_file in html_files:
        content = z.read(html_file).decode("utf-8", errors="ignore")
        if section_title in content:
            return content

    return None


def get_business_overview(rcept_no: str) -> list[str]:
    """
    사업보고서에서 '사업의 내용' 섹션을 추출합니다.
    30,000자 초과 시 청크 리스트로 분할 반환합니다.
    """
    z = _fetch_document_zip(rcept_no)
    html = _extract_section_html(z, _BUSINESS_SECTION)
    if not html:
        return []

    text = _clean_html(html)
    if len(text) <= _CHUNK_SIZE:
        return [text]
    return [text[i:i + _CHUNK_SIZE] for i in range(0, len(text), _CHUNK_SIZE)]

def get_disclosure_document(rcept_no: str) -> str:
    """
    주요사항보고서(B 유형) 원문을 4,000자까지 추출합니다.
    단일 파일 구조라 ZIP 내 가장 큰 HTML 파일을 바로 읽습니다.
    """
    z = _fetch_document_zip(rcept_no)
    filename = _largest_html_in_zip(z)
    html = z.read(filename).decode("utf-8", errors="ignore")
    text = _clean_html(html)
    return text[:_B_TYPE_MAX_CHARS]