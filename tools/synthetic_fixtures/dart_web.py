"""Synthetic DART website fixtures (mode 'web'): main.do with its TOC script and two viewer.do sections.

The company (한빛전자, "Hanbit Electronics"), receipt number, document numbers, offsets and all body text are
invented. The TOC headings are the standard business-report form headings (사업보고서 서식), which every filer uses.
The page chrome copies only the JS idioms the parser must survive (var nodeN = {}, nodeN['field'] = "value",
trailing comments, children push, a later viewDoc(...) call). Run: python3 tools/synthetic_fixtures/dart_web.py
"""
from __future__ import annotations

from pathlib import Path

FIX = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
RCPT, DCM = "20260310009990", "9990488"
MAIN, OVERVIEW, PRODUCTS = (f"dart_web_main_{RCPT}.html", f"dart_web_section_overview_{RCPT}.html",
                            f"dart_web_section_products_{RCPT}.html")

HEADINGS = """
1|사 업 보 고 서
1|【 대표이사 등의 확인 】
1|I. 회사의 개요
2|1. 회사의 개요
2|2. 회사의 연혁
2|3. 자본금 변동사항
2|4. 주식의 총수 등
2|5. 정관에 관한 사항
1|II. 사업의 내용
2|1. 사업의 개요
2|2. 주요 제품 및 서비스
2|3. 원재료 및 생산설비
2|4. 매출 및 수주상황
2|5. 위험관리 및 파생거래
2|6. 주요계약 및 연구개발활동
2|7. 기타 참고사항
1|III. 재무에 관한 사항
2|1. 요약재무정보
2|2. 연결재무제표
3|2-1. 연결 재무상태표
3|2-2. 연결 손익계산서
3|2-3. 연결 포괄손익계산서
3|2-4. 연결 자본변동표
3|2-5. 연결 현금흐름표
2|3. 연결재무제표 주석
3|1. 일반적 사항 (연결)
3|2. 중요한 회계처리방침 (연결)
3|3. 중요한 회계추정 및 가정 (연결)
3|4. 범주별 금융상품 (연결)
3|5. 금융자산의 양도 (연결)
3|6. 공정가치금융자산 (연결)
3|7. 매출채권 및 미수금 (연결)
3|8. 재고자산 (연결)
3|9. 관계기업 및 공동기업 투자 (연결)
3|10. 유형자산 (연결)
3|11. 무형자산 (연결)
3|12. 차입금 (연결)
3|13. 사채 (연결)
3|14. 순확정급여부채(자산) (연결)
3|15. 충당부채 (연결)
3|16. 우발부채와 약정사항 (연결)
3|17. 계약부채 (연결)
3|18. 자본금 (연결)
3|19. 연결이익잉여금 (연결)
3|20. 기타자본항목 (연결)
3|21. 비용의 성격별 분류 (연결)
3|22. 판매비와관리비 (연결)
3|23. 기타수익 및 기타비용 (연결)
3|24. 금융수익 및 금융비용 (연결)
3|25. 법인세비용 (연결)
3|26. 주당이익 (연결)
3|27. 현금흐름표 (연결)
3|28. 재무위험관리 (연결)
3|29. 공정가치 측정 (연결)
3|30. 부문별 보고 (연결)
3|31. 특수관계자와의 거래 (연결)
3|32. 비지배지분 (연결)
3|33. 사업결합 (연결)
3|34. 보고기간후사건 (연결)
2|4. 재무제표
3|4-1. 재무상태표
3|4-2. 손익계산서
3|4-3. 포괄손익계산서
3|4-4. 자본변동표
3|4-5. 현금흐름표
2|5. 재무제표 주석
3|1. 일반적 사항
3|2. 중요한 회계처리방침
3|3. 중요한 회계추정 및 가정
3|4. 범주별 금융상품
3|5. 금융자산의 양도
3|6. 공정가치금융자산
3|7. 매출채권 및 미수금
3|8. 재고자산
3|9. 종속기업, 관계기업 및 공동기업 투자
3|10. 유형자산
3|11. 무형자산
3|12. 차입금
3|13. 사채
3|14. 순확정급여부채(자산)
3|15. 충당부채
3|16. 우발부채와 약정사항
3|17. 계약부채
3|18. 자본금
3|19. 이익잉여금
3|20. 기타자본항목
3|21. 비용의 성격별 분류
3|22. 판매비와관리비
3|23. 기타수익 및 기타비용
3|24. 금융수익 및 금융비용
3|25. 법인세비용
3|26. 주당이익
3|27. 현금흐름표
3|28. 재무위험관리
3|29. 공정가치 측정
3|30. 부문별 보고
3|31. 특수관계자와의 거래
3|32. 보고기간후사건
2|6. 배당에 관한 사항
2|7. 증권의 발행을 통한 자금조달에 관한 사항
2|8. 기타 재무에 관한 사항
1|IV. 이사의 경영진단 및 분석의견
2|1. 예측정보에 대한 주의사항
2|2. 개요
2|3. 재무상태 및 영업실적
2|4. 유동성 및 자금조달과 지출
2|5. 부외거래
2|6. 그 밖에 투자의사결정에 필요한 사항
1|V. 회계감사인의 감사의견 등
2|1. 외부감사에 관한 사항
2|2. 내부통제에 관한 사항
1|VI. 이사회 등 회사의 기관에 관한 사항
2|1. 이사회에 관한 사항
2|2. 감사제도에 관한 사항
2|3. 주주총회 등에 관한 사항
1|VII. 주주에 관한 사항
1|VIII. 임원 및 직원 등에 관한 사항
2|1. 임원 및 직원 등의 현황
2|2. 임원의 보수 등
1|IX. 계열회사 등에 관한 사항
1|X. 대주주 등과의 거래내용
1|XI. 그 밖에 투자자 보호를 위하여 필요한 사항
2|1. 공시내용 진행 및 변경사항
2|2. 우발부채 등에 관한 사항
2|3. 제재 등과 관련된 사항
2|4. 작성기준일 이후 발생한 주요사항 등 기타사항
1|XII. 상세표
2|1. 연결대상 종속회사 현황(상세)
2|2. 계열회사 현황(상세)
2|3. 타법인출자 현황(상세)
2|4. 연구개발실적(상세)
1|【 전문가의 확인 】
2|1. 전문가의 확인
2|2. 전문가와의 이해관계
""".strip().splitlines()

# Section lengths (bytes of the viewer.do fragment); the business chapter and its two sections are fixed so the
# tests can pin them: II. 사업의 내용 = eleId 9, 1. 사업의 개요 = eleId 10, 2. 주요 제품 및 서비스 = eleId 11.
LENGTHS = {"II. 사업의 내용": 88412, "1. 사업의 개요": 1873, "2. 주요 제품 및 서비스": 3310}


def toc_nodes() -> list[dict]:
    out, offset = [], 812
    for i, line in enumerate(HEADINGS, start=1):
        level, text = line.split("|", 1)
        length = LENGTHS.get(text, 400 + (i * 7919) % 20000)
        out.append({"level": int(level), "text": text, "id": str(i), "eleId": str(i), "offset": str(offset),
                    "length": str(length), "atocId": str(i if i > 1 else 402)})
        offset += 136 if text in ("II. 사업의 내용",) else length + 4
    return out


def main_page() -> str:
    parts, stack = [], []
    for n in toc_nodes():
        lv, ind = n["level"], "\t" * n["level"]
        while stack and stack[-1] >= lv:
            closing = stack.pop()
            parts.append(f"{ind[:-1] if closing > 1 else ind}" + (
                f"node{closing - 1}['children'].push(node{closing});\n" if closing > 1 else "treeData.push(node1);\n"))
        if lv > 1:
            parts.append(f"{ind}if (!node{lv - 1}['children']) node{lv - 1}['children'] = [];\n")
        parts.append(f"{ind}var node{lv} = {{}};\n")
        parts.append(f"{ind}node{lv}['text'] = \"{n['text']}\";\n")
        parts.append(f"{ind}node{lv}['id'] = \"{n['id']}\"; \t\n")
        for k in ("rcpNo", "dcmNo", "eleId", "offset", "length", "dtd"):
            v = {"rcpNo": RCPT, "dcmNo": DCM, "dtd": "dart4.xsd"}.get(k) or n[k]
            parts.append(f"{ind}node{lv}['{k}'] = \"{v}\";\n")
        parts.append(f"{ind}node{lv}['tocNo'] =  \"{n['eleId']}\";  //eleId may differ from the TOC order\n")
        parts.append(f"{ind}node{lv}['atocId'] =  \"{n['atocId']}\";  //editor TOC id\n{ind}\n{ind}cnt++;\n")
        stack.append(lv)
    while stack:
        closing = stack.pop()
        parts.append(("\t" * closing) + (f"node{closing - 1}['children'].push(node{closing});\n" if closing > 1
                                           else "treeData.push(node1);\n"))
    first = toc_nodes()[0]
    return (
        "\n\n<!DOCTYPE html>\n<html lang=\"ko\">\n<head>\n<title>한빛전자/사업보고서/2026.03.10</title>\n"
        "<meta charset=\"UTF-8\">\n<script type=\"text/javascript\" src=\"/js/jquery/synthetic.js\"></script>\n"
        "<script type=\"text/javascript\">\nfunction loadHistoryContents() {\n\txajax.simpleSend(\"/x.ax\");\n}\n"
        "function initPage() {\n\t//목차생성 및 최초뷰어호출\n\tmakeToc();\n\twindow.focus();\n}\n\n"
        "function makeToc() {\n\tcnt = 0;\n\tvar treeData = [];\n\t\n" + "".join(parts) +
        "\n\tjsTree.on('loaded.jstree', function(){\n\t\tjsTree.on(\"select_node.jstree\", function (e, data) {\n"
        "\t\t\tvar original = data.node.original;\n\t\t\tviewDoc(original.rcpNo, original.dcmNo, original.eleId, "
        "original.offset, original.length, original.dtd, original.tocNo);\n\t\t});\n\t});\n"
        f"\t\t\tviewDoc(\"{RCPT}\", \"{DCM}\", \"1\", \"{first['offset']}\", \"{first['length']}\", \"dart4.xsd\", \"\");\n"
        "}\n\nfunction viewDoc(rcpNo, dcmNo, eleId, offset, length, dtd, tocNo) {\n"
        "\tvar node1 = {};\n\tnode1['text'] = 'viewer state, not a TOC entry';\n}\n"
        "</script>\n</head>\n<body onload=\"initPage()\"><div class=\"leftPanel\"></div></body>\n</html>\n")


HEAD = ("<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.01 Transitional//EN\" \"http://www.w3.org/TR/html4/loose.dtd\">\n"
        "<HTML style='border:0'>\n<HEAD>\n<TITLE></TITLE>\n"
        "<META http-equiv=\"Content-Type\" content=\"text/html; charset=utf-8\">\n"
        "<link rel=\"stylesheet\" type=\"text/css\" href=\"/css/report_xml.css\">\n</HEAD>\n<BODY bgcolor=\"#FFFFFF\">\n"
        "<P><BR/></P>\n")
TAIL = "<P class='pgbrk'></P>\n<P><BR/></P>\n</BODY>\n</HTML>\n"


def overview_page() -> str:
    return HEAD + (
        "<P class='section-2'><A name='toc1'>1. 사업의 개요</A></P>\n<P><BR/></P>\n"
        "<P>당사는 본사를 거점으로 한국과 해외 3개 지역총괄의 생산ㆍ판매법인 등 27개의 종속기업으로 구성된 "
        "글로벌 전자 기업입니다.<BR/><BR/></P>\n"
        "<P>사업별로 보면, 부품 사업은 센서 모듈, 전원 모듈, 산업용 카메라 등을 생산ㆍ판매하고 있습니다.<BR/>"
        "또한, 자회사 한빛오디오에서는 차량용 스피커와 앰프를 개발, 생산, 판매하고 있습니다.<BR/><BR/>"
        "☞ 부문별 사업에 관한 자세한 사항은 '7. 기타 참고사항'의 '다. 사업부문별 현황'과 &nbsp; "
        "'라. 사업부문별 요약 재무 현황' 항목을 참고하시기 바랍니다.<BR/><BR/></P>\n"
        "<P>지역별로 보면, 국내에서는 본사와 9개의 종속기업이 사업을 운영하고 있으며, 본사는 청주와 "
        "&nbsp;구미 사업장으로 구성되어 있습니다.</P>\n<P><BR/></P>\n"
        "<P>해외에서는 생산, 판매, 연구활동 등을 담당하는 18개의 비상장 종속기업이 운영되고 있습니다.<BR/><BR/>"
        "주요 경쟁사로는 Aster Devices, Borealis Parts, Cobalt Sensing 등(알파벳순)이 있습니다.</P>\n"
        "<P><BR/></P>\n") + TAIL


def _row(cells: list[str], heights=("30", "23")) -> str:
    tds = "".join(f"  <TD height='{heights[1]}'{a}>{c}</TD>\n" for c, a in cells)
    return f"<TR height='{heights[0]}'>\n{tds}</TR>\n"


def products_page() -> str:
    head = "".join(f"  <TH height='23' valign='TOP'>{h}</TH>\n" for h in ("부 &nbsp;문", "주요 제품", "매출액", "비중"))
    body = "".join(_row(r) for r in (
        [("완제품 부문", " align='CENTER'"), ("센서 모듈, 전원 모듈,<BR/>산업용 카메라, 제어 보드 등", ""),
         ("18,796", " align='RIGHT'"), ("56.3%", " align='RIGHT'")],
        [("부품 부문", " align='CENTER'"), ("커넥터, 수동소자 등", ""), ("13,012", " align='RIGHT'"),
         ("39.0%", " align='RIGHT'")],
        [("한빛오디오", " align='CENTER'"), ("차량용 스피커, 앰프 등", ""), ("4,523", " align='RIGHT'"),
         ("13.6%", " align='RIGHT'")],
        [("기타", " align='CENTER'"), ("부문간 내부거래 제거 등", ""), ("△2,971", " align='RIGHT'"),
         ("△8.9%", " align='RIGHT'")],
        [("총 계", " colspan='2' align='CENTER'"), ("33,360", " align='RIGHT'"), ("100.00%", " align='RIGHT'")],
    ))
    return HEAD + (
        "<P class='section-2'><A name='toc1'>2. 주요 제품 및 서비스</A></P>\n<P><BR/></P>\n"
        "<P><SPAN style='font-size:14pt;font-weight:bold;'>가. 주요 제품 매출<BR/><BR/></SPAN><SPAN>당사는 센서 모듈, "
        "전원 모듈 등 완제품과 커넥터 등 부품을 생산ㆍ판매하고 있습니다.<BR/><BR/>2025년 매출은 완제품 부문이 "
        "1조 8,796억원(56.3%), 부품 부문이 1조 3,012억원(39.0%)입니다.</SPAN><BR/>\n</P>\n"
        "<TABLE class='nb' width='790'>\n<TBODY>\n<TR height='30'>\n"
        "  <TD height='23' align='RIGHT' valign='TOP'>(단위 : 억원, %)</TD>\n</TR>\n</TBODY>\n</TABLE>\n"
        f"<TABLE border='1' width='791'>\n<THEAD>\n<TR height='30'>\n{head}</TR>\n</THEAD>\n<TBODY>\n{body}</TBODY>\n"
        "</TABLE>\n<TABLE class='nb' width='789'>\n<TBODY>\n"
        + _row([("※ 각 부문별 매출액은 부문 등 간 내부거래를 포함하고 있습니다.", " valign='TOP'"),
                ("&nbsp;[△는 부(-)의 값임]", " align='RIGHT' valign='TOP'")])
        + _row([("※ 세부 제품별 매출은 '4. 매출 및 수주상황' 항목을 참고하시기 바랍니다.", " valign='TOP'"),
                ("<BR/>", " align='RIGHT' valign='TOP'")])
        + "</TBODY>\n</TABLE>\n<P><BR/></P>\n"
        "<P><SPAN style='font-size:14pt;font-weight:bold;'>나. 주요 제품 등의 가격 변동 현황</SPAN><SPAN><BR/><BR/>"
        "2025년 센서 모듈의 평균 판매가격은 전년 대비 약 4% 하락하였으며, 전원 모듈은 전년과 유사한 "
        "수준입니다.<BR/></SPAN><BR/>\n</P>\n") + TAIL


def main() -> None:
    (FIX / MAIN).write_text(main_page(), encoding="utf-8")
    (FIX / OVERVIEW).write_text(overview_page(), encoding="utf-8")
    (FIX / PRODUCTS).write_text(products_page(), encoding="utf-8")


if __name__ == "__main__":
    main()
