# 공시검색 MCP 배포 체크리스트

## 자동 완료

- `dist/공시검색-MCP-0.3.5.mcpb` 생성
- `dist/사용설명서.html` 생성
- `dist/SHA256SUMS.txt` 생성
- MCPB manifest v0.4 schema validation 통과
- 패키지 info 읽기 성공
- 전체 자동시험 `189 passed, 20 subtests passed`
- 고정 평가 `24/24`
- OpenDART API 키·Cookie·감사로그·fixture 미포함 검증

## 사용자 최종 확인

> 가장 먼저 `server_version`을 확인하세요. 버전이 다르면 이전 설치가 남아 있는 것이며, 그 상태로는 결과를 평가하지 마세요.

1. 재설치·완전 재시작 후 첫 도구 호출 결과의 `server_version`이 설치한 버전(현재 0.3.5)과 같은지 확인한다. 다르면 확장을 제거하고 다시 설치한다.
2. Claude Desktop을 최신 버전으로 업데이트한다.
3. 설정 → 확장에서 **확장 설치(Install Extension…)**로 `dist/공시검색-MCP-0.3.5.mcpb`를 선택한다.
4. OpenDART API 인증키 40자리를 입력한다.
5. Claude Desktop을 완전히 종료한 뒤 다시 실행한다.
6. 커넥터 또는 도구 목록에 공시검색 MCP가 보이는지 확인한다.
7. 속도우선 기능으로 “2025년 1월 1일부터 2025년 12월 31일까지 상계납입 사례를 찾아줘.”를 실행하고 90초 내 응답과 verified 결과를 확인한다.
8. 각 결과에 DART 공시 원문 링크가 함께 표시되는지 확인한다.
9. 범위가 넓은 질문에서 심화 검색기능 안내가 “대화형 검색예산” 표현 없이 표시되는지 확인한다.
10. `preview_batch_research`가 예상 범위와 시간을 먼저 보여주고 바로 원문을 다운로드하지 않는지 확인한다.
11. 공개 배포 전 코드 서명 필요 여부와 라이선스를 결정한다.

## 배포 파일

- 패키지: `dist/공시검색-MCP-0.3.5.mcpb`
- 안내서: `dist/사용설명서.html`
- 체크섬: `dist/SHA256SUMS.txt`

SHA-256:

```text
ac89a63938be13f184b68832e414e39f381618f8e6e047d61ebfdf424f1a068c  공시검색-MCP-0.3.5.mcpb
```
